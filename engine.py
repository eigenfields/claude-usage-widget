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
from datetime import datetime, timezone, timedelta, time, date
from pathlib import Path
from zoneinfo import ZoneInfo

if sys.stdout:                       # None under pythonw (the run.vbs launcher)
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.txt"
POINTS_PATH = HERE / "points.json"
UTC = timezone.utc

# $/MTok (input, output). cache_write = 1.25*input.
# cache_read = 0.01*input — NOT the 0.10 API billing ratio. Re-derived
# 2026-07-07 from 23 real /usage readings: a grid search over (cr, cw, out)
# weights collapsed the within-session drift (implied %/$ slid 0.60->0.28 under
# cr=0.10) and cut session fit error from RMS 2.20 to 0.57, with the weekly
# pairs (1%..52%) as the out-of-sample check (RMS 0.84). The subscription
# limiter charges cached context far below API price — without this, cache-
# heavy sessions (>150k context, subagents) overstate by ~4pts mid-session.
PRICE = {"fable": (10.0, 50.0), "opus": (5.0, 25.0),
         "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}
CR, CW = 0.01, 1.25
# Usage credits bill at STANDARD API rates — including the API's 10x-higher
# cache-read weight — so the credits estimator prices with these, never CR/CW.
API_CR, API_CW = 0.10, 1.25
MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
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
# Max-20x account (n=11/12 fit under the CR=0.01 weights, 2026-07-07); other
# tiers scaled by their limit ratio vs 20x (Max 5x = 1/4 the budget, Pro =
# 1/20). The first real /usage paste replaces these with the account's own fit.
TIER_MULT = {"max20x": 1.0, "max5x": 4.0, "pro": 20.0}
BASE_A = {"session": 0.6681, "week_all": 0.1153,
          "week_fable_A": 0.3365, "week_fable_B": 0.1608}

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

# credits (optional) : tunes the Fable usage-credits estimator gauge.
#   credits_cap       = your monthly usage-credits spend cap in dollars, e.g. 100
#   credits_from      = the date Fable started billing usage credits for you
#                       (YYYY-MM-DD; blank = start of the current credits month)
#   credits_reset_day = day of month your credits reset (default 1)
credits_cap =
credits_from =
credits_reset_day = 1

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


def fam_name(model):
    ml = (model or "").lower()
    if "fable" in ml or "mythos" in ml: return "fable"
    if "opus" in ml:   return "opus"
    if "sonnet" in ml: return "sonnet"
    if "haiku" in ml:  return "haiku"
    return None

def price_for(model):
    f = fam_name(model)
    return PRICE[f] if f else None

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
                fresh = u.get("input_tokens", 0) or 0
                cread = u.get("cache_read_input_tokens", 0) or 0
                cwrite = u.get("cache_creation_input_tokens", 0) or 0
                seen[key] = dict(
                    ts=parse_ts(ts), model=model, is_sub=bool(d.get("isSidechain")),
                    sid=d.get("sessionId"), agent=d.get("attributionAgent"),
                    ctx=fresh + cread + cwrite,
                    fresh_input=fresh,
                    output=u.get("output_tokens", 0) or 0,
                    cache_read=cread,
                    cache_creation=cwrite,
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

def comp_over(records, start_utc, end_utc):
    """Per-model-family token composition over a window:
    {family: [fresh_input, cache_read, cache_creation, output]}."""
    comp = {}
    for r in records:
        if start_utc <= r["ts"] < end_utc:
            fam = fam_name(r["model"])
            if fam is None:
                continue
            c = comp.setdefault(fam, [0, 0, 0, 0])
            c[0] += r["fresh_input"]; c[1] += r["cache_read"]
            c[2] += r["cache_creation"]; c[3] += r["output"]
    return comp

def comp_cost(comp, keep=None, cr=None, cw=None):
    """Cost-equivalent $ of a stored composition — under the subscription
    limiter weights by default (calibration pairs derive from composition at
    fit time, so retuning CR/CW/prices re-fits all history without logs), or
    under explicit weights (cr=API_CR, cw=API_CW prices a composition at what
    usage credits would actually bill)."""
    cr = CR if cr is None else cr
    cw = CW if cw is None else cw
    tot = 0.0
    for fam, c in (comp or {}).items():
        if keep is not None and fam not in keep:
            continue
        pin, pout = PRICE[fam]
        tot += pin * (c[0] + cr * c[1] + cw * c[2]) + pout * c[3]
    return tot * 1e-6


def insights(records, now):
    """Local recomputation of /usage's 'behaviors' panel over 24h and 7d:
    request/session counts plus cost-equivalent shares (subagent work, big-
    context work, long-running sessions, top subagent types). Sessions =
    distinct sessionIds; shares are of cost, not request count."""
    out = {}
    for key, span in (("d1", timedelta(hours=24)), ("d7", timedelta(days=7))):
        rs = [r for r in records if r["ts"] >= now - span]
        tot = sum(rec_cost(r) for r in rs) or 1.0
        sess = {}
        for r in rs:
            s = sess.setdefault(r["sid"], [r["ts"], r["ts"]])
            if r["ts"] < s[0]: s[0] = r["ts"]
            if r["ts"] > s[1]: s[1] = r["ts"]
        long_ids = {k for k, (a, b) in sess.items() if b - a >= timedelta(hours=8)}
        agents = {}
        for r in rs:
            if r["agent"]:
                agents[r["agent"]] = agents.get(r["agent"], 0.0) + rec_cost(r)
        out[key] = {
            "req": len(rs),
            "sessions": len(sess),
            "sub_share": round(100 * sum(rec_cost(r) for r in rs if r["is_sub"]) / tot),
            "big_ctx_share": round(100 * sum(rec_cost(r) for r in rs if r["ctx"] >= 150_000) / tot),
            "long_share": round(100 * sum(rec_cost(r) for r in rs if r["sid"] in long_ids) / tot),
            "top_agents": [[a, round(100 * c / tot)] for a, c in
                           sorted(agents.items(), key=lambda kv: -kv[1])[:3] if c / tot >= 0.005],
        }
    return out

FABLE_ONLY = {"fable"}          # hypothesis A model set
PREMIUM = {"fable", "opus"}     # hypothesis B model set


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
    cfg = {"logs_path": "", "plan": "max20x", "credits_cap": "",
           "credits_from": "", "credits_reset_day": "1", "text": text, "mtime": mtime}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*(logs_path|plan|credits_cap|credits_from|credits_reset_day)"
                     r"\s*=\s*(.*?)\s*$", line, re.I)
        if m:
            cfg[m.group(1).lower()] = m.group(2)
    cfg["plan"] = (cfg["plan"] or "max20x").lower().replace(" ", "")
    if cfg["plan"] not in TIER_MULT:
        cfg["plan"] = "max20x"
    try:
        cap = float(str(cfg["credits_cap"]).replace("$", "").replace(",", "").strip())
        cfg["credits_cap"] = cap if cap > 0 else None
    except (ValueError, TypeError):
        cfg["credits_cap"] = None
    try:
        cfg["credits_from"] = date.fromisoformat(str(cfg["credits_from"]).strip())
    except (ValueError, TypeError):
        cfg["credits_from"] = None
    try:
        cfg["credits_reset_day"] = max(1, min(28, int(str(cfg["credits_reset_day"]).strip())))
    except (ValueError, TypeError):
        cfg["credits_reset_day"] = 1
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
    """'Jul 4, 11:59pm' / 'Jul 7, 7:09pm' (+ optional IANA tz) -> the true
    boundary instant, UTC. /usage displays the last usable MINUTE of the
    window ('11:59pm' -> midnight, '7:09pm' -> 7:10 — session blocks anchor to
    the first request's minute, not the hour), except an exact :00 display,
    which IS the boundary ('12am', '5pm'). So: minute != 0 -> +1min; :00 stays.
    No year is printed: pick the candidate nearest the capture time (Dec/Jan
    safe)."""
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
    if d.minute:                         # last-usable-minute display -> boundary
        d += timedelta(minutes=1)
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
    """Turn one parsed /usage block captured at T into a calibration point
    (schema v2): the raw token COMPOSITION of each window plus its real %, and
    the reset anchors the block pins. Composition is frozen at ingest so later
    transcript cleanup can't corrode it, but dollars are derived from it at fit
    time — so a weight/price retune re-fits all history without the logs.
    A window is only taken when T verifiably lies inside it."""
    pt = {"captured": T.isoformat(), "hash": block["hash"]}
    s = block.get("session")
    if s and s.get("reset_raw"):
        end = _parse_reset(s["reset_raw"], s.get("tz"), T)
        if end:
            pt["session_anchor"] = end.isoformat()
            start = end - timedelta(hours=SESSION_HOURS)
            if start <= T <= end:
                pt["session"] = {"comp": comp_over(records, start, T), "pct": s["pct"]}
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
                w = {"comp": comp_over(records, wstart, T)}
                if wk:
                    w["all_pct"] = wk["pct"]
                if prem:
                    w["fable_pct"] = prem["pct"]
                    w["label"] = prem.get("label")   # whatever /usage calls it
                pt["week"] = w
    return pt


def _contradicts_floor(pct, implied):
    """True when a pasted reading is impossibly LOW against what local cost
    alone implies. Local logs are a floor (real >= local), so a reading far
    below the floor-implied % means the provider reset/rebased its counter
    mid-window (limits migration, promo rollover — observed 2026-07-09) and
    the window semantics don't match: the pair would be junk. Deliberately
    narrow — moderately-low readings are legitimate scale changes the fit
    must LEARN, not reject."""
    return implied >= 4.0 and pct <= max(1.0, implied / 4.0)


def _quarantine(ptn, fits):
    """Strip readings that contradict the local floor from a freshly derived
    point (reset anchors are kept — they're valid regardless). Only guards
    buckets with at least one real calibration point: a tier default must
    never be trusted enough to reject a stranger's first paste.
    Returns the list of dropped readings."""
    dropped = []
    s = ptn.get("session")
    if s and fits["session"]["n"]:
        f = fits["session"]
        if _contradicts_floor(s["pct"], f["a"] * comp_cost(s["comp"]) + f["floor"]):
            del ptn["session"]; dropped.append("session")
    w = ptn.get("week")
    if w:
        f = fits["week_all"]
        if (f["n"] and w.get("all_pct") is not None and
                _contradicts_floor(w["all_pct"], f["a"] * comp_cost(w["comp"]) + f["floor"])):
            del ptn["week"]; dropped.append("week")
        else:
            fa = fits["week_fable_A"]
            if (fa["n"] and w.get("fable_pct") is not None and
                    _contradicts_floor(w["fable_pct"],
                                       fa["a"] * comp_cost(w["comp"], keep=FABLE_ONLY) + fa["floor"])):
                w.pop("fable_pct"); dropped.append("fable")
    return dropped


def ingest_pastes(cfg, records, store):
    """Fold any not-yet-seen /usage paste in config.txt into the points store.
    Capture time = config.txt's save time, so only the bottom-most new block
    (the one just pasted and saved) can be timed correctly; any older unseen
    blocks are marked seen and skipped. Readings that contradict the local
    floor are quarantined (see _contradicts_floor). Returns the list of
    quarantined readings ([] when none, also [] when nothing was ingested)."""
    seen = set(store["seen_hashes"])
    fresh, fh = [], set()
    for b in parse_usage_blocks(cfg["text"]):
        if b["hash"] not in seen and b["hash"] not in fh:
            fresh.append(b)
            fh.add(b["hash"])
    if not fresh:
        return []
    for b in fresh[:-1]:
        store["seen_hashes"].append(b["hash"])
    b = fresh[-1]
    ptn = derive_point(b, cfg["mtime"], records)
    quarantined = _quarantine(ptn, fits_from(store, cfg["plan"]))
    store["points"].append(ptn)
    store["seen_hashes"].append(b["hash"])
    if ptn.get("session_anchor"):
        store["session_anchor"] = ptn["session_anchor"]
    if ptn.get("week_anchor"):
        store["week_anchor"] = ptn["week_anchor"]
    if (ptn.get("week") or {}).get("label"):
        store["premium_label"] = ptn["week"]["label"]   # gauge 3 follows /usage
    save_points(store)
    return quarantined


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
    """Per-bucket fit from stored points — (cost, %) pairs are derived from each
    point's stored composition under the CURRENT weights. The premium bucket is
    derived under both hypotheses (A: fable-only, B: opus-class) from the same
    week composition. A bucket with no points yet falls back to the plan-tier
    default scale (always provisional)."""
    pairs = {k: [] for k in BASE_A}
    for p in store["points"]:
        s = p.get("session")
        if s and s.get("comp") is not None:
            x = comp_cost(s["comp"])
            if x > 0:
                pairs["session"].append((x, s["pct"]))
        w = p.get("week")
        if w and w.get("comp") is not None:
            xw = comp_cost(w["comp"])
            if w.get("all_pct") is not None and xw > 0:
                pairs["week_all"].append((xw, w["all_pct"]))
            if w.get("fable_pct") is not None:
                xa = comp_cost(w["comp"], keep=FABLE_ONLY)
                xb = comp_cost(w["comp"], keep=PREMIUM)
                if xa > 0:
                    pairs["week_fable_A"].append((xa, w["fable_pct"]))
                if xb > 0:
                    pairs["week_fable_B"].append((xb, w["fable_pct"]))
    mult = TIER_MULT.get(plan, 1.0)
    return {k: (fit_through_origin(v) if v else
                {"a": BASE_A[k] * mult, "sigma": None, "floor": 0.0, "n": 0})
            for k, v in pairs.items()}


# ---- usage-credits month + daily ledger ---------------------------------------
def _month_step(d, months):
    y, m = d.year, d.month - 1 + months
    return date(y + m // 12, m % 12 + 1, d.day)

def credits_period(now_utc, reset_day=1):
    """[start, end) of the current usage-credits month: local midnight on the
    reset day (the billing timezone isn't knowable locally — an honest, clearly
    'est.' approximation)."""
    tz = local_tz()
    loc = now_utc.astimezone(tz)
    sd = date(loc.year, loc.month, min(reset_day, 28))
    if loc.date() < sd:
        sd = _month_step(sd, -1)
    ed = _month_step(sd, 1)
    def mk(dd):
        return datetime.combine(dd, time(0, 0), tzinfo=tz).astimezone(UTC)
    return mk(sd), mk(ed)

def update_day_ledger(store, records, from_utc, now_utc):
    """Freeze a per-day token composition for every CLOSED local day since
    `from_utc` (immutable once written), so the monthly credits estimate never
    needs logs older than yesterday — and survives the ~30-day transcript
    cleanup mid-month. Returns True when the store changed."""
    tz = local_tz()
    led = store.setdefault("day_comps", {})
    d = from_utc.astimezone(tz).date()
    today = now_utc.astimezone(tz).date()
    changed = False
    while d < today:
        k = d.isoformat()
        if k not in led:
            s = datetime.combine(d, time(0, 0), tzinfo=tz).astimezone(UTC)
            e = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(UTC)
            led[k] = comp_over(records, s, e)
            changed = True
        d += timedelta(days=1)
    horizon = (today - timedelta(days=62)).isoformat()
    for k in [k for k in led if k < horizon]:
        del led[k]
        changed = True
    return changed


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

    All rolling is UTC-absolute (DST-free); a re-anchored resume is floored to
    its MINUTE — blocks anchor to the first request's minute, not the hour
    (observed: a 2:09pm off-device start displaying 'resets 7:09pm').
    Returns (None, None, True) when idle."""
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
                    spt = ts.astimezone(local_tz()).replace(second=0, microsecond=0)
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
                spt = ts.astimezone(local_tz()).replace(second=0, microsecond=0)
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
    m = f":{l.minute:02d}" if l.minute else ""      # mid-hour resets are real
    return f"{h}{m}{'am' if l.hour < 12 else 'pm'}"

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
    # ...and far enough to freeze any credits-month days the ledger is missing
    # (first run reaches back to the period start; steady state adds nothing).
    tzl = local_tz()
    cp_start, cp_end = credits_period(now, cfg["credits_reset_day"])
    c_from = cp_start
    if cfg["credits_from"]:
        c_from = max(c_from, datetime.combine(cfg["credits_from"], time(0, 0),
                                              tzinfo=tzl).astimezone(UTC))
    led = store.get("day_comps", {})
    dmiss = c_from.astimezone(tzl).date()
    today_l = now.astimezone(tzl).date()
    while dmiss < today_l and dmiss.isoformat() in led:
        dmiss += timedelta(days=1)
    if dmiss < today_l:
        since = min(since, datetime.combine(dmiss, time(0, 0), tzinfo=tzl).astimezone(UTC))
    if records is None:
        records, _, _ = load_records(logs, since)
    quarantined = ingest_pastes(cfg, records, store)
    if update_day_ledger(store, records, c_from, now):
        save_points(store)
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

    # ---- Fable usage-credits estimate: month-to-date, priced at API rates ----
    cred = 0.0
    dd = c_from.astimezone(tzl).date()
    while dd < today_l:
        c = store.get("day_comps", {}).get(dd.isoformat())
        if c:
            cred += comp_cost(c, keep=FABLE_ONLY, cr=API_CR, cw=API_CW)
        dd += timedelta(days=1)
    live_from = max(c_from, datetime.combine(today_l, time(0, 0), tzinfo=tzl).astimezone(UTC))
    if live_from < now:
        cred += comp_cost(comp_over(records, live_from, now),
                          keep=FABLE_ONLY, cr=API_CR, cw=API_CW)
    cap = cfg["credits_cap"]
    c_pt = round(min(100.0, 100.0 * cred / cap)) if cap else 0
    c_proj = None
    if cap and now > c_from:
        fr = (now - c_from).total_seconds() / (cp_end - c_from).total_seconds()
        if 0.08 <= fr < 1.0 and cred > 0:
            p = round(100.0 * (cred / fr) / cap)
            c_proj = min(p, 200) if p > c_pt else None
    c_danger = bool(cap and (cred >= cap or (c_proj or 0) >= 90))
    dollars = f"${cred:,.2f}" if cred < 10 else f"${cred:,.0f}"
    cp_end_l = cp_end.astimezone(tzl)

    plabel = ((store.get("premium_label") or "fable").strip().title() or "Fable")
    req7 = sum(1 for r in records if r["ts"] >= now - timedelta(days=7))
    lu = now.astimezone(local_tz())
    h12 = lu.hour % 12 or 12
    updated = f"{MON[lu.month-1]} {lu.day}, {h12}:{lu.minute:02d}{'am' if lu.hour < 12 else 'pm'}"
    caveat = ("Local Claude Code logs only — excludes web/mobile/other devices, "
              "so real usage can only be higher.")
    if not store["points"]:
        caveat += " Paste /usage into config.txt once to calibrate."
    if quarantined:
        caveat += (f" Skipped the paste's {'/'.join(quarantined)} reading(s) — "
                   "they contradict local logs (provider likely reset its counters).")
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
            {"key": "week_fable", "label": plabel, "sub": "weekly",
             "point": fp, "projected": f_proj, "danger": _danger(fp, f_proj),
             "provisional": fpv or w_soft, "reset": week_reset},
            # always provisional: real credit billing is invisible locally — this
            # prices the local token record at API rates (a floor, like the rest)
            {"key": "credits", "label": "Credits", "sub": "Fable, monthly",
             "point": c_pt, "projected": c_proj, "danger": c_danger,
             "provisional": True, "dollars": dollars, "cap": cap,
             "reset": f"{MON[cp_end_l.month-1]} {cp_end_l.day}"},
        ],
        "n_snapshots": len(store["points"]),
        "req_7d": req7,
        "updated": updated,
        "logs_dir": str(logs),
        "insights": insights(records, now),
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
