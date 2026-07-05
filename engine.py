"""
Claude Code usage engine — reads local transcripts, dedups to true billable
requests, and computes cost-equivalent load per usage window.

Verified foundation (workflow forensics, 2026-07-04):
  - dedup by requestId (fallback message.id); uuid does NOT dedup (per-line-write)
  - include sidechains (subagents); exclude model == "<synthetic>"
  - sum TOP-LEVEL message.usage only (never the nested `iterations` array)
  - deduped 7d count vs /usage's own request count  ->  ~0.6% match

Self-contained data layer (no separate calibrate step):
  - find_logs_dir() locates ~/.claude/projects on any OS; config can override
  - config.txt is the ONE user-facing file: optional logs_path/plan up top,
    raw /usage pastes below; capture time = the file's save time
  - new pastes are ingested into points.json (internal) on any refresh and the
    % = a*cost fit recomputes live; zero pastes -> plan-tier default scales
"""
import sys, os, re, json, glob, math, hashlib
from datetime import datetime, timezone, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

if sys.stdout:                       # None under pythonw (the run.vbs launcher)
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.txt"
POINTS_PATH = HERE / "points.json"
UTC = timezone.utc

# $/MTok (input, output). cache_read = 0.10*input, cache_write = 1.25*input.
PRICE = {"fable": (10.0, 50.0), "opus": (5.0, 25.0),
         "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
CR, CW = 0.10, 1.25
SESSION_HOURS = 5
# A session block's first local request can legitimately lag its start by normal
# think-time; only a lag beyond this marks the window soft (off-device/mid-block
# start). The hybrid handles the phase — this only warns, so err loose.
SESSION_LATE_SOFT_MINUTES = 75
# No usage window looks back further than 7 days (+ slack), so transcript files
# last touched before this can't contribute and are skipped unopened.
LOOKBACK_DAYS = 8

FABLE_HYP = "A"   # A = Fable-5 only ; B = Opus-class. Flip once pastes decide.

# Zero-paste fallback scales (% per cost-equivalent $) so the widget shows a
# sane provisional number straight after download. Derived from one calibrated
# Max-20x account (n=3 fit, 2026-07-04); other tiers scaled by their limit
# ratio vs 20x (Max 5x = 1/4 the budget, Pro = 1/20). The first real /usage
# paste replaces these with the account's own fit.
TIER_MULT = {"max20x": 1.0, "max5x": 4.0, "pro": 20.0}
BASE_A = {"session": 0.3995, "week_all": 0.05945,
          "week_fable_A": 0.18183, "week_fable_B": 0.08537}

CONFIG_TEMPLATE = """\
# Claude Usage Widget - config (the only file you ever edit)
#
# logs_path : normally leave BLANK - the widget finds your Claude Code logs by
#             itself (~/.claude/projects on every OS, or $CLAUDE_CONFIG_DIR).
#             Set it only if the widget says it couldn't find them, e.g.
#               logs_path = C:\\Users\\you\\.claude\\projects
#
# plan      : your subscription tier - max20x | max5x | pro
#             Used only until your first /usage paste calibrates the gauges
#             to your real account; after that, pastes win.

logs_path =
plan = max20x

# ---------------- calibration: paste /usage output below ----------------
# Run /usage in Claude Code, copy the WHOLE output, paste it below this line,
# and save the file right away (the save time anchors the reading - while the
# widget is running it picks the paste up within seconds).
# Paste a fresh reading whenever you like: each one tightens the fit and
# re-pins your session/weekly reset times. Overwrite the old paste or leave
# it - readings the widget has already seen are skipped automatically.
"""


def local_tz():
    """The system's current UTC offset (fresh per call, so DST flips are picked
    up without a restart). Used only for display and top-of-hour flooring; all
    window math stays UTC-absolute."""
    return datetime.now().astimezone().tzinfo


def price_for(model):
    ml = (model or "").lower()
    if "fable" in ml or "mythos" in ml: return PRICE["fable"]
    if "opus" in ml:   return PRICE["opus"]
    if "sonnet" in ml: return PRICE["sonnet"]
    if "haiku" in ml:  return PRICE["haiku"]
    return None

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- log discovery -----------------------------------------------------------
def find_logs_dir(override=None):
    """Locate the Claude Code transcript root. Precedence: config override ->
    $CLAUDE_CONFIG_DIR -> ~/.claude/projects (same idea on every OS) -> XDG.
    Prefers a candidate that actually contains *.jsonl; falls back to any that
    exists (a fresh install is legitimately empty); None if nothing exists."""
    cands = []
    if override:
        p = Path(os.path.expandvars(os.path.expanduser(str(override))))
        cands += [p, p / "projects"]                 # accept ~/.claude itself too
    ccd = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if ccd:
        cands += [Path(ccd) / "projects", Path(ccd)]
    cands.append(Path.home() / ".claude" / "projects")
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        cands.append(Path(xdg) / "claude" / "projects")
    cands.append(Path.home() / ".config" / "claude" / "projects")
    existing = [c for c in cands if c.is_dir()]
    for c in existing:
        if next(c.rglob("*.jsonl"), None) is not None:
            return c
    return existing[0] if existing else None


def load_records(logs_dir, since=None):
    """One deduped record per billable request: dict(ts, model, is_sub, tokens...).
    `since` skips files not touched since then (a file's records can't be newer
    than its mtime, so this is loss-free for windowed math)."""
    seen = {}
    unknown_models = {}
    files = []
    for path in glob.glob(os.path.join(str(logs_dir), "**", "*.jsonl"), recursive=True):
        if since is not None:
            try:
                if os.path.getmtime(path) < since.timestamp():
                    continue
            except OSError:
                continue
        files.append(path)
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message")
                if not isinstance(m, dict) or "usage" not in m:
                    continue
                model = m.get("model")
                if model == "<synthetic>":
                    continue
                key = d.get("requestId") or m.get("id")
                if not key or key in seen:
                    continue
                ts = d.get("timestamp")
                if not ts:
                    continue
                if price_for(model) is None:
                    unknown_models[model] = unknown_models.get(model, 0) + 1
                u = m["usage"]
                seen[key] = dict(
                    ts=parse_ts(ts), model=model, is_sub=bool(d.get("isSidechain")),
                    fresh_input=u.get("input_tokens", 0) or 0,
                    output=u.get("output_tokens", 0) or 0,
                    cache_read=u.get("cache_read_input_tokens", 0) or 0,
                    cache_creation=u.get("cache_creation_input_tokens", 0) or 0,
                )
    return list(seen.values()), files, unknown_models

def rec_cost(r):
    p = price_for(r["model"])
    if p is None:
        return 0.0
    pin, pout = p
    eff_in = r["fresh_input"] + CR * r["cache_read"] + CW * r["cache_creation"]
    return (pin * eff_in + pout * r["output"]) * 1e-6

def is_fable_strict(model):   # hypothesis A: literally Fable 5
    return "fable" in (model or "").lower() or "mythos" in (model or "").lower()

def is_premium(model):        # hypothesis B: Opus-class premium (opus + fable)
    ml = (model or "").lower()
    return "fable" in ml or "mythos" in ml or "opus" in ml

def cost_over(records, start_utc, end_utc, keep=None):
    tot = 0.0
    for r in records:
        if start_utc <= r["ts"] < end_utc and (keep is None or keep(r["model"])):
            tot += rec_cost(r)
    return tot


# ---- the ONE config file -----------------------------------------------------
def load_config():
    """config.txt: created from the template on first run; the widget itself
    never writes it again. That rule is load-bearing — the file's mtime doubles
    as the capture time of a newly pasted /usage block, so only the user's own
    save may ever touch it."""
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8", errors="replace")
        mtime = datetime.fromtimestamp(CONFIG_PATH.stat().st_mtime, tz=UTC)
    except OSError:
        text, mtime = "", datetime.now(UTC)
    cfg = {"logs_path": "", "plan": "max20x", "text": text, "mtime": mtime}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*(logs_path|plan)\s*=\s*(.*?)\s*$", line, re.I)
        if m:
            cfg[m.group(1).lower()] = m.group(2)
    cfg["plan"] = (cfg["plan"] or "max20x").lower().replace(" ", "")
    if cfg["plan"] not in TIER_MULT:
        cfg["plan"] = "max20x"
    return cfg


# ---- /usage paste parsing ----------------------------------------------------
MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}
_RE_USAGE = re.compile(r"current\s+(session|week)\s*(?:\(([^)]*)\))?\s*:\s*(\d+)\s*%", re.I)
_RE_RESET = re.compile(r"resets:?\s+([^(\n]+?)\s*(?:\(([^)\n]+)\))?\s*$", re.I)

def parse_usage_blocks(text):
    """Scan arbitrary text for pasted /usage blocks. A block = a 'Current
    session' line plus the 'Current week' lines that follow it (a week-only
    fragment before any session line is accepted too). Returned in file order;
    each carries a content hash so a reading is only ever ingested once."""
    blocks, cur = [], None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = _RE_USAGE.search(line)
        if not m:
            continue
        kind = m.group(1).lower()
        label = (m.group(2) or "").strip().lower()
        r = _RE_RESET.search(line)
        entry = {"pct": int(m.group(3)),
                 "reset_raw": (r.group(1).strip() if r else None),
                 "tz": (r.group(2).strip() if r and r.group(2) else None)}
        if kind == "session":
            if cur:
                blocks.append(cur)
            cur = {"session": entry, "lines": [line]}
        else:
            if cur is None:
                cur = {"lines": []}
            cur["lines"].append(line)
            if "all" in label or label == "":
                cur["week_all"] = entry
            else:                        # Fable / Opus / whatever the plan shows
                cur.setdefault("premium", {**entry, "label": label})
    if cur:
        blocks.append(cur)
    for b in blocks:
        basis = "|".join(re.sub(r"\s+", " ", l).lower() for l in b["lines"])
        b["hash"] = hashlib.sha256(basis.encode()).hexdigest()[:16]
    return blocks


def _parse_reset(raw, tzname, ref):
    """'Jul 4, 11:59pm' (+ optional IANA tz) -> the true boundary instant, UTC.
    /usage displays the last usable minute (X:59) of a reset that sits on the
    hour, so the boundary = displayed time +1min, floored to the hour (also a
    no-op for an exact on-the-hour display). No year is printed: pick the
    candidate nearest the capture time (Dec/Jan safe)."""
    if not raw:
        return None
    try:
        tz = ZoneInfo(tzname) if tzname else local_tz()
    except Exception:
        tz = local_tz()
    s = raw.strip().rstrip(".")
    md = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})", s)
    tm = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", s, re.I)
    if not tm:
        return None
    hh = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "pm" else 0)
    mm = int(tm.group(2) or 0)
    ref_l = ref.astimezone(tz)
    if md and md.group(1)[:3].lower() in MONTHS:
        mo, day = MONTHS[md.group(1)[:3].lower()], int(md.group(2))
        cands = []
        for y in (ref_l.year - 1, ref_l.year, ref_l.year + 1):
            try:
                cands.append(datetime(y, mo, day, hh, mm, tzinfo=tz))
            except ValueError:
                pass
        if not cands:
            return None
        d = min(cands, key=lambda c: abs(c - ref_l))
    else:                                # bare time: next occurrence after capture
        d = ref_l.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d <= ref_l:
            d += timedelta(days=1)
    d = (d + timedelta(minutes=1)).replace(minute=0, second=0, microsecond=0)
    return d.astimezone(UTC)


# ---- calibration points store (internal; never user-edited) -------------------
def load_points():
    try:
        d = json.loads(POINTS_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    d.setdefault("version", 1)
    d.setdefault("seen_hashes", [])
    d.setdefault("points", [])
    return d

def save_points(d):
    tmp = POINTS_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        os.replace(tmp, POINTS_PATH)
    except OSError:
        pass


def derive_point(block, T, records):
    """Turn one parsed /usage block captured at T into a calibration point:
    (cost-in-window, real %) pairs per bucket, plus the reset anchors it pins.
    Pairs are frozen at ingest so later transcript cleanup can't corrode the
    fit; a pair is only taken when T verifiably lies inside its window."""
    pt = {"captured": T.isoformat(), "hash": block["hash"], "pairs": {}}
    s = block.get("session")
    if s and s.get("reset_raw"):
        end = _parse_reset(s["reset_raw"], s.get("tz"), T)
        if end:
            pt["session_anchor"] = end.isoformat()
            start = end - timedelta(hours=SESSION_HOURS)
            if start <= T <= end:
                pt["pairs"]["session"] = [cost_over(records, start, T), s["pct"]]
    wk, prem = block.get("week_all"), block.get("premium")
    wsrc = wk or prem or {}
    if wsrc.get("reset_raw"):
        wend = _parse_reset(wsrc["reset_raw"], wsrc.get("tz"), T)
        if wend:
            try:
                tz = ZoneInfo(wsrc["tz"]) if wsrc.get("tz") else local_tz()
            except Exception:
                tz = local_tz()
            wl = wend.astimezone(tz)
            pt["week_anchor"] = {"tz": wsrc.get("tz"), "weekday": wl.weekday(),
                                 "hour": wl.hour, "minute": wl.minute}
            wstart = datetime.combine(wl.date() - timedelta(days=7),
                                      time(wl.hour, wl.minute),
                                      tzinfo=tz).astimezone(UTC)
            if wstart <= T <= wend:
                if wk:
                    pt["pairs"]["week_all"] = [cost_over(records, wstart, T), wk["pct"]]
                if prem:
                    pt["pairs"]["week_fable_A"] = [
                        cost_over(records, wstart, T, keep=is_fable_strict), prem["pct"]]
                    pt["pairs"]["week_fable_B"] = [
                        cost_over(records, wstart, T, keep=is_premium), prem["pct"]]
    return pt


def ingest_pastes(cfg, records, store):
    """Fold any not-yet-seen /usage paste in config.txt into the points store.
    Capture time = config.txt's save time, so only the bottom-most new block
    (the one just pasted and saved) can be timed correctly; any older unseen
    blocks are marked seen and skipped. Returns the number of points added."""
    seen = set(store["seen_hashes"])
    fresh, fh = [], set()
    for b in parse_usage_blocks(cfg["text"]):
        if b["hash"] not in seen and b["hash"] not in fh:
            fresh.append(b)
            fh.add(b["hash"])
    if not fresh:
        return 0
    for b in fresh[:-1]:
        store["seen_hashes"].append(b["hash"])
    b = fresh[-1]
    ptn = derive_point(b, cfg["mtime"], records)
    store["points"].append(ptn)
    store["seen_hashes"].append(b["hash"])
    if ptn.get("session_anchor"):
        store["session_anchor"] = ptn["session_anchor"]
    if ptn.get("week_anchor"):
        store["week_anchor"] = ptn["week_anchor"]
    save_points(store)
    return 1


def fit_through_origin(pairs):
    """pairs: list of (x_cost, y_pct). Returns a, sigma, floor, n."""
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    a = (sxy / sxx) if sxx > 0 else 0.0
    res = [y - a * x for x, y in zip(xs, ys)]
    n = len(pairs)
    sigma = (sum(r * r for r in res) / (n - 1)) ** 0.5 if n >= 2 else None
    floor = max(0.0, sum(res) / n) if n else 0.0
    return {"a": a, "sigma": sigma, "floor": floor, "n": n}


def fits_from(store, plan):
    """Per-bucket fit from stored points; a bucket with no points yet falls back
    to the plan-tier default scale (always provisional)."""
    pairs = {k: [] for k in BASE_A}
    for p in store["points"]:
        for k, pr in (p.get("pairs") or {}).items():
            if k in pairs and pr and pr[0] > 0:
                pairs[k].append(tuple(pr))
    mult = TIER_MULT.get(plan, 1.0)
    return {k: (fit_through_origin(v) if v else
                {"a": BASE_A[k] * mult, "sigma": None, "floor": 0.0, "n": 0})
            for k, v in pairs.items()}


# ---- window boundaries -------------------------------------------------------
def weekly_window(now_utc, anchor=None):
    """(start, end, soft) of the weekly limit window. `anchor` (pinned by the
    latest /usage paste) is the account's fixed weekly reset — {tz, weekday,
    hour, minute}, recurring at local wall time. With no anchor yet, fall back
    to the trailing 7 days (soft: an over-count of the true window, since the
    real one started at most 7 days ago)."""
    if not anchor:
        return now_utc - timedelta(days=7), None, True
    try:
        tz = ZoneInfo(anchor["tz"]) if anchor.get("tz") else local_tz()
    except Exception:
        tz = local_tz()
    hhmm = time(anchor.get("hour", 0), anchor.get("minute", 0))
    loc = now_utc.astimezone(tz)
    d = loc.date() - timedelta(days=(loc.date().weekday() - anchor.get("weekday", 0)) % 7)
    start = datetime.combine(d, hhmm, tzinfo=tz)
    if start > loc:                                    # before this week's boundary
        start = datetime.combine(d - timedelta(days=7), hhmm, tzinfo=tz)
    end = datetime.combine(start.date() + timedelta(days=7), hhmm, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC), False


def session_window(now_utc, records, anchor=None):
    """5h session block containing `now`, per the real mechanic: a block is
    [s, s+5h); the next block's s is the first request at/after expiry. `anchor`
    (a known reset from the latest /usage paste, UTC) seeds the phase so the
    current chain lands on the true grid even when its first request was off-device
    (e.g. a session started on claude.ai/mobile). Idle gaps that outlast a block
    re-anchor to the resume request, mirroring an actual reset. Falls back to a
    local gap-walk when no anchor exists. Returns (start, end, soft) where `soft`
    is True whenever the window is under-corroborated (walk fallback, re-anchored,
    or the block's first local request lands well past its start).

    All rolling is UTC-absolute (DST-free); local tz is touched only to floor a
    re-anchored resume to the hour. Returns (None, None, True) when idle."""
    H = timedelta(hours=SESSION_HOURS)

    if anchor is not None:
        def grid_start(ts):                       # UTC-absolute; DST-free
            n = math.floor((ts - anchor).total_seconds() / H.total_seconds())
            return anchor + n * H
        # Only the CURRENT chain matters. Walk requests from the block the anchor
        # was read in (anchor - 5h) or now's grid block, whichever is earlier;
        # older history belongs to prior chains and must not drag the phase.
        lb = min(anchor - H, grid_start(now_utc))
        msgs = sorted(t for t in (r["ts"] for r in records) if lb <= t <= now_utc)
        s = e = None
        on_grid = True
        for ts in msgs:
            if e is None:                         # first request -> snap onto grid
                s = grid_start(ts); e = s + H
            elif ts >= e:                         # previous block expired at e
                gs = grid_start(ts)
                if gs == e:                       # resume in the next grid slot
                    s, e = e, e + H               #   -> continuous, stay on grid
                else:                             # idle skipped >=1 slot -> re-anchor
                    spt = ts.astimezone(local_tz()).replace(minute=0, second=0, microsecond=0)
                    s = spt.astimezone(UTC); e = s + H
                    on_grid = False
        if e is None:                             # no local requests in this chain
            s = grid_start(now_utc); e = s + H
        while now_utc >= e:                        # idle tail up to now: roll forward
            s, e = e, e + H
            on_grid = False
        start, end, src = s, e, ("anchor" if on_grid else "rewalk")
    else:
        msgs = sorted(t for t in (r["ts"] for r in records) if t <= now_utc)
        s = e = None                              # ultimate fallback: local gap-walk
        for ts in msgs:
            if e is None or ts >= e:
                spt = ts.astimezone(local_tz()).replace(minute=0, second=0, microsecond=0)
                s = spt.astimezone(UTC); e = s + H
        if e is None or now_utc >= e:
            return None, None, True               # no active session (idle)
        start, end, src = s, e, "walk"

    first = min((t for t in msgs if start <= t < now_utc), default=None)
    late = first is None or (first - start) > timedelta(minutes=SESSION_LATE_SOFT_MINUTES)
    return start, end, (src != "anchor") or late


# ---- live gauge computation (consumed by the widget) ------------------------
def _fmt_hour(dt):
    l = dt.astimezone(local_tz()); h = l.hour % 12 or 12
    return f"{h}{'am' if l.hour < 12 else 'pm'}"

def _fmt_week(dt):
    l = dt.astimezone(local_tz()); h = l.hour % 12 or 12
    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][l.weekday()]
    return f"{wd} {h}:{l.minute:02d}{'am' if l.hour < 12 else 'pm'}"

def _predict(cost, fit):
    a = fit.get("a", 0.0) or 0.0
    floor = fit.get("floor", 0.0) or 0.0
    sigma = fit.get("sigma", None)
    point = max(0.0, min(100.0, a * cost + floor))
    band = sigma if sigma is not None else 15.0
    hi = max(0.0, min(100.0, point + max(band, floor)))
    return point, hi, (sigma is None or (fit.get("n", 0) or 0) < 3)


def _project(point, start, end, now):
    """Burn-rate projection: extrapolate current usage to the window's reset.
    Returns None when too little of the window has elapsed to project."""
    if start is None or end is None:
        return None
    total = (end - start).total_seconds()
    elapsed = (now - start).total_seconds()
    if total <= 0 or elapsed <= 0:
        return None
    frac = elapsed / total
    if frac < 0.08 or frac >= 1.0:
        return None                          # too early to project (or window over)
    proj = round(point / frac)
    return min(proj, 200) if proj > point else None


def compute_gauges(now=None, records=None):
    now = now or datetime.now(UTC)
    cfg = load_config()
    logs = find_logs_dir(cfg["logs_path"])
    if logs is None:
        return {"error": "couldn't find your Claude Code logs — set logs_path in "
                         "config.txt (next to app.py)"}
    store = load_points()
    # Look back far enough for the newest un-ingested paste's week window, which
    # can predate now-8d if the widget wasn't running when the paste was saved.
    since = now - timedelta(days=LOOKBACK_DAYS)
    seen = set(store["seen_hashes"])
    if any(b["hash"] not in seen for b in parse_usage_blocks(cfg["text"])):
        since = min(since, cfg["mtime"] - timedelta(days=LOOKBACK_DAYS))
    if records is None:
        records, _, _ = load_records(logs, since)
    ingest_pastes(cfg, records, store)
    fits = fits_from(store, cfg["plan"])

    anchor = None                                  # session phase = latest paste's reset
    sa = store.get("session_anchor")
    if sa:
        try:
            anchor = datetime.fromisoformat(sa)
        except Exception:
            anchor = None
    w_st, w_end, w_soft = weekly_window(now, store.get("week_anchor"))
    s_st, s_end, s_soft = session_window(now, records, anchor)
    keep_fable = is_fable_strict if FABLE_HYP == "A" else is_premium
    cost_sess = cost_over(records, s_st, now) if s_st else 0.0
    cost_all = cost_over(records, w_st, now)
    cost_fable = cost_over(records, w_st, now, keep=keep_fable)
    fkey = "week_fable_A" if FABLE_HYP == "A" else "week_fable_B"
    sp, _, spv = _predict(cost_sess, fits["session"])
    ap, _, apv = _predict(cost_all, fits["week_all"])
    fp, _, fpv = _predict(cost_fable, fits[fkey])
    sp, ap, fp = round(sp), round(ap), round(fp)
    # ghost arc = burn-rate projection to the window's reset (None if too early)
    s_proj = _project(sp, s_st, s_end, now)
    w_proj = _project(ap, w_st, w_end, now)
    f_proj = _project(fp, w_st, w_end, now)

    def _danger(pt, proj):
        return bool((proj is not None and proj >= 90) or pt > 100)

    week_reset = _fmt_week(w_end) if w_end else "unknown"
    req7 = sum(1 for r in records if r["ts"] >= now - timedelta(days=7))
    lu = now.astimezone(local_tz())
    mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][lu.month-1]
    h12 = lu.hour % 12 or 12
    updated = f"{mon} {lu.day}, {h12}:{lu.minute:02d}{'am' if lu.hour < 12 else 'pm'}"
    caveat = ("Local Claude Code logs only — excludes web/mobile/other devices, "
              "so real usage can only be higher.")
    if not store["points"]:
        caveat += " Paste /usage into config.txt once to calibrate."
    return {
        "gauges": [
            {"key": "session", "label": "Session", "sub": "5-hour window",
             "point": sp, "projected": s_proj, "danger": _danger(sp, s_proj),
             # window-soft (off-device/mid-block start, re-anchored, or no paste
             # anchor) also trips provisional, so a mis-scoped window can't render
             # as a confident wrong number; the "~" prefix marks an uncertain reset.
             "provisional": spv or s_soft,
             "reset": (("~" if s_soft else "") + _fmt_hour(s_end)) if s_end else "idle"},
            {"key": "week_all", "label": "Week", "sub": "all models",
             # weekly-soft = no paste has pinned the account's weekly reset yet, so
             # the window is the trailing 7d (an over-count) and the reset unknown.
             "point": ap, "projected": w_proj, "danger": _danger(ap, w_proj),
             "provisional": apv or w_soft, "reset": week_reset},
            {"key": "week_fable", "label": "Fable", "sub": "weekly",
             "point": fp, "projected": f_proj, "danger": _danger(fp, f_proj),
             "provisional": fpv or w_soft, "reset": week_reset},
        ],
        "n_snapshots": len(store["points"]),
        "req_7d": req7,
        "updated": updated,
        "logs_dir": str(logs),
        "caveat": caveat,
    }


# ---- diagnostics: py engine.py -----------------------------------------------
def main():
    cfg = load_config()
    logs = find_logs_dir(cfg["logs_path"])
    print(f"config:  {CONFIG_PATH}   (plan={cfg['plan']}"
          f"{', logs_path=' + cfg['logs_path'] if cfg['logs_path'] else ''})")
    print(f"logs:    {logs if logs else 'NOT FOUND — set logs_path in config.txt'}")
    if logs is None:
        return
    now = datetime.now(UTC)
    records, files, unknown = load_records(logs, now - timedelta(days=LOOKBACK_DAYS))
    print(f"files touched <{LOOKBACK_DAYS}d: {len(files)}  |  deduped billable requests: {len(records):,}")
    if unknown:
        print(f"  !! unknown models (priced as $0): {unknown}")
    r7 = [r for r in records if r["ts"] >= now - timedelta(days=7)]
    r24 = [r for r in records if r["ts"] >= now - timedelta(hours=24)]
    print(f"  rolling 24h requests: {len(r24):,}   rolling 7d: {len(r7):,}   (compare to /usage)")
    by_model = {}
    for r in r7:
        by_model[r["model"]] = by_model.get(r["model"], 0) + 1
    print("  7d requests by model:")
    for mdl, c in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"    {mdl:28} {c:>6,}")

    store = load_points()
    print(f"\ncalibration points: {len(store['points'])}")
    print(f"  session_anchor: {store.get('session_anchor')}")
    print(f"  week_anchor:    {store.get('week_anchor')}")
    fits = fits_from(store, cfg["plan"])
    hdr = f"  {'bucket':14} {'n':>2} {'a (%/$)':>9} {'sigma':>7} {'floor':>6} {'budget($)':>10}"
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for k, fit in fits.items():
        a = fit["a"]; bud = (100.0 / a) if a > 0 else float("nan")
        sig = f"{fit['sigma']:.2f}" if fit["sigma"] is not None else "  -"
        tag = "" if fit["n"] else f"   (default for plan={cfg['plan']})"
        print(f"  {k:14} {fit['n']:>2} {a:>9.4f} {sig:>7} {fit['floor']:>6.2f} {bud:>10.1f}{tag}")

    g = compute_gauges(now=now, records=records)
    print("\nlive gauges:")
    for x in g["gauges"]:
        print(f"  {x['label']:8} {x['point']:>3}%"
              f"{'  est.' if x['provisional'] else '      '}"
              f"   resets {x['reset']}"
              + (f"   -> projected {x['projected']}%" if x.get("projected") else ""))
    print(f"  ({g['req_7d']:,} req/7d · updated {g['updated']})")

if __name__ == "__main__":
    main()
