"""Pins for the engine's empirical rules — the parts a refactor would silently
break: /usage reset parsing, paste-block scanning, the through-origin fit and
its regime window, the quarantine thresholds, weekly-window DST behavior, and
the transcript snapshot dedup. Pure functions only; no user files are touched
(pricing lookups are pointed at temp files so list-price refreshes can never
break the suite).

Run:  py -m pytest tests/
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine  # noqa: E402

UTC = timezone.utc
LA = "America/Los_Angeles"


@pytest.fixture
def no_pricing(monkeypatch, tmp_path):
    """Point the resolver at a missing pricing.json -> hardcoded PRICE table,
    so cost assertions are stable constants, immune to weekly list refreshes."""
    monkeypatch.setattr(engine, "PRICING_PATH", tmp_path / "missing.json")
    engine._PCACHE.update(mtime=None, data=None, keys=(), standing=None)
    yield
    engine._PCACHE.update(mtime=None, data=None, keys=(), standing=None)


@pytest.fixture
def toy_pricing(monkeypatch, tmp_path):
    """A known pricing.json for resolver-behavior tests."""
    p = tmp_path / "pricing.json"
    p.write_text(json.dumps({
        "cache_multipliers": {"read": 0.10, "write": 1.25},
        "models": {
            "opus-4-1": {"input": 15, "output": 75},
            "opus": {"input": 5, "output": 25},
            "sonnet-5": [{"until": "2026-08-31", "input": 2, "output": 10},
                         {"input": 3, "output": 15}],
        },
        "family_fallback": {"opus": "opus", "sonnet": "sonnet-5"},
    }), encoding="utf-8")
    monkeypatch.setattr(engine, "PRICING_PATH", p)
    engine._PCACHE.update(mtime=None, data=None, keys=(), standing=None)
    yield
    engine._PCACHE.update(mtime=None, data=None, keys=(), standing=None)


# ---- _parse_reset: the displayed-minute rules ---------------------------------
def test_reset_nonzero_minute_is_last_usable_so_plus_one():
    ref = datetime(2026, 7, 9, 21, 0, tzinfo=UTC)
    end = engine._parse_reset("Jul 9, 3:49pm", LA, ref)
    assert end == datetime(2026, 7, 9, 22, 50, tzinfo=UTC)  # 3:50pm PDT

def test_reset_1159_rolls_to_midnight():
    ref = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    end = engine._parse_reset("Jul 4, 11:59pm", LA, ref)
    assert end == datetime(2026, 7, 5, 7, 0, tzinfo=UTC)    # 12:00am PDT Jul 5

def test_reset_exact_hour_is_the_boundary():
    ref = datetime(2026, 7, 9, 21, 0, tzinfo=UTC)
    end = engine._parse_reset("Jul 12, 5pm", LA, ref)
    assert end == datetime(2026, 7, 13, 0, 0, tzinfo=UTC)   # 5:00pm PDT, no +1

def test_reset_year_rollover_picks_nearest():
    ref = datetime(2026, 12, 30, 12, 0, tzinfo=UTC)
    end = engine._parse_reset("Jan 2, 5pm", LA, ref)
    assert end.astimezone(engine.ZoneInfo(LA)).year == 2027

def test_reset_bare_time_means_next_occurrence():
    ref = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)           # 5:00am PDT
    end = engine._parse_reset("3:49pm", LA, ref)
    assert end == datetime(2026, 7, 9, 22, 50, tzinfo=UTC)


# ---- parse_usage_blocks: the real /usage paste format -------------------------
PASTE = """\
Current session: 7% used · resets Jul 9, 3:49pm (America/Los_Angeles)
Current week (all models): 2% used · resets Jul 12, 4:59pm (America/Los_Angeles)
Current week (Fable): 3% used · resets Jul 12, 4:59pm (America/Los_Angeles)
"""

def test_paste_block_parses_all_three_lines():
    (b,) = engine.parse_usage_blocks(PASTE)
    assert b["session"]["pct"] == 7
    assert b["session"]["reset_raw"] == "Jul 9, 3:49pm"
    assert b["session"]["tz"] == LA
    assert b["week_all"]["pct"] == 2
    assert b["premium"]["pct"] == 3 and b["premium"]["label"] == "fable"

def test_paste_hash_survives_whitespace_and_double_paste():
    (b1,) = engine.parse_usage_blocks(PASTE)
    (b2,) = engine.parse_usage_blocks(PASTE.replace(" used", "  used"))
    assert b1["hash"] == b2["hash"]                 # ingest-once must hold
    assert len(engine.parse_usage_blocks(PASTE + "\n" + PASTE)) == 2

def test_week_only_fragment_is_a_block():
    blocks = engine.parse_usage_blocks(PASTE.splitlines()[1])
    assert len(blocks) == 1 and blocks[0]["week_all"]["pct"] == 2


# ---- fit_through_origin + the regime window -----------------------------------
def test_fit_basics():
    assert engine.fit_through_origin([])["a"] == 0.0
    one = engine.fit_through_origin([(2.0, 4.0)])
    assert one["a"] == 2.0 and one["n"] == 1 and one["sigma"] is None
    exact = engine.fit_through_origin([(1, 2), (2, 4), (3, 6)])
    assert abs(exact["a"] - 2.0) < 1e-12 and exact["floor"] == 0.0

def test_fit_window_learns_a_regime_change(no_pricing):
    mtok = 1_000_000                       # 1M fresh sonnet tokens = $3 standing
    old = [{"session": {"comp": {"sonnet": [mtok, 0, 0, 0]}, "pct": 3}}] * 8
    new = [{"session": {"comp": {"sonnet": [mtok, 0, 0, 0]}, "pct": 6}}] * engine.FIT_WINDOW
    store = {"points": old + new}
    fit = engine.fits_from(store, "max20x")["session"]
    assert fit["n"] == engine.FIT_WINDOW    # regression eats only the freshest N
    assert abs(fit["a"] - 2.0) < 1e-9       # 6% / $3 — old regime fully flushed

def test_no_points_falls_back_to_tier_default():
    fit = engine.fits_from({"points": []}, "pro")["session"]
    assert fit["n"] == 0
    assert fit["a"] == pytest.approx(engine.BASE_A["session"] * engine.TIER_MULT["pro"])


# ---- quarantine thresholds -----------------------------------------------------
@pytest.mark.parametrize("pct,implied,junk", [
    (1, 4.0, True),      # at the floor of the guard
    (2, 4.0, False),     # moderately low = legitimate scale change: must LEARN
    (10, 41.0, True),    # quarter rule
    (10, 39.9, False),
    (0, 3.9, False),     # implied too small to trust the guard at all
])
def test_contradicts_floor(pct, implied, junk):
    assert engine._contradicts_floor(pct, implied) is junk

def test_quarantine_never_guards_tier_defaults(no_pricing):
    fits = engine.fits_from({"points": []}, "max20x")     # n=0 everywhere
    ptn = {"session": {"comp": {"sonnet": [10_000_000, 0, 0, 0]}, "pct": 0}}
    assert engine._quarantine(ptn, fits) == []            # a stranger's first
    assert "session" in ptn                               # paste is sacred


# ---- weekly window: wall-clock anchor across DST -------------------------------
def test_weekly_window_dst_end_week_is_169_hours():
    anchor = {"tz": LA, "weekday": 6, "hour": 17, "minute": 0}   # Sun 5pm local
    inside = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)           # DST ends Nov 1
    ws, we, soft = engine.weekly_window(inside, anchor)
    assert not soft
    assert (we - ws) == timedelta(hours=169)

def test_weekly_window_normal_week_is_168_hours():
    anchor = {"tz": LA, "weekday": 6, "hour": 17, "minute": 0}
    ws, we, _ = engine.weekly_window(datetime(2026, 7, 9, 12, 0, tzinfo=UTC), anchor)
    assert (we - ws) == timedelta(hours=168)
    assert ws <= datetime(2026, 7, 9, 12, 0, tzinfo=UTC) < we


# ---- transcript snapshot dedup --------------------------------------------------
def _line(req, out, ts, model="claude-sonnet-5", typ="assistant"):
    return json.dumps({
        "type": typ, "timestamp": ts, "requestId": req, "sessionId": "s1",
        "message": {"model": model, "usage": {
            "input_tokens": 3, "output_tokens": out,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}},
    })

def test_dedup_keeps_final_snapshot_with_start_time(no_pricing, tmp_path):
    f = tmp_path / "proj" / "t.jsonl"
    f.parent.mkdir()
    f.write_text("\n".join([
        _line("r1", 38, "2026-07-09T10:00:00Z"),     # progressive snapshots:
        _line("r1", 309, "2026-07-09T10:00:05Z"),    # output grows, rest fixed
        _line("r2", 7, "2026-07-09T10:01:00Z"),
        _line("r3", 9, "2026-07-09T10:02:00Z", model="<synthetic>"),
        json.dumps({"type": "user", "note": 'mentions "usage" but is not billable'}),
    ]), encoding="utf-8")
    recs, files, unknown = engine.load_records(tmp_path)
    assert len(recs) == 2 and not unknown
    r1 = next(r for r in recs if r["output"] == 309)
    assert r1["ts"] == datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)   # first ts kept
    assert r1["cache_read"] == 100 and r1["fresh_input"] == 3

def test_unknown_model_counted_once_priced_zero(no_pricing, tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join([
        _line("r1", 5, "2026-07-09T10:00:00Z", model="claude-nova-1"),
        _line("r1", 50, "2026-07-09T10:00:05Z", model="claude-nova-1"),
    ]), encoding="utf-8")
    recs, _, unknown = engine.load_records(tmp_path)
    assert unknown == {"claude-nova-1": 1}
    assert engine.rec_cost(recs[0]) == 0.0


# ---- insights: no phantom session from sid-less records -------------------------
def test_sessionless_records_dont_collapse_into_one_phantom():
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    base = dict(model="claude-sonnet-5", is_sub=False, agent=None, ctx=0,
                fresh_input=1000, output=10, cache_read=0, cache_creation=0)
    recs = [dict(base, ts=now - timedelta(hours=1), sid="a"),
            dict(base, ts=now - timedelta(hours=2), sid=None),
            dict(base, ts=now - timedelta(hours=3), sid=None)]
    ins = engine.insights(recs, now)
    assert ins["d1"]["sessions"] == 1          # only the attributable one
    assert ins["d1"]["req"] == 3               # but every request still counts


# ---- route (b): the two credit boundaries and the no-double-billing clamp -------
# Fixed stage: weeks anchor Mon 00:00 UTC (2026-07-13 is a Monday), one ledger
# day Tue Jul 14, "now" Wed Jul 15 noon. Comps are fresh-tokens-only, so
# subscription-$ and API-repriced-$ coincide under the hardcoded PRICE table
# (fable 10/50, haiku 1/5) and the arithmetic is checkable by hand.
WANCHOR = {"tz": "UTC", "weekday": 0, "hour": 0, "minute": 0}

def _fit(a):
    return {"a": a, "sigma": 0.5, "floor": 0.0, "n": 12}

_OFF = {"a": 0.0, "sigma": None, "floor": 0.0, "n": 0}

def _overage(day_fable_usd=0.0, day_other_usd=0.0, a_week=None, a_fable=None):
    from datetime import date as _date
    comp = {}
    if day_fable_usd:
        comp["fable"] = [int(day_fable_usd / 10.0 * 1_000_000), 0, 0, 0]
    if day_other_usd:
        comp["haiku"] = [int(day_other_usd / 1.0 * 1_000_000), 0, 0, 0]
    fits = {"week_all": _fit(a_week) if a_week else _OFF,
            "week_fable_A": _fit(a_fable) if a_fable else _OFF,
            "session": _OFF, "week_fable_B": _OFF}
    return engine._overage_credits(
        store={"day_comps": {"2026-07-14": comp}},
        fits=fits, wanchor=WANCHOR,
        cp_start=datetime(2026, 7, 1, tzinfo=UTC),
        now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        today_comp={}, tzl=UTC, fable_from=_date.max)

def test_fable_subcap_bills_only_beyond_its_boundary():
    # Bf = 100/10 = $10; $20 of Fable -> $10 past the cap, general leg off
    over = _overage(day_fable_usd=20, a_fable=10.0)
    assert set(over) == {"fable"}
    assert over["fable"] == pytest.approx(10.0)

def test_capped_fable_cannot_push_the_weekly_pool_over():
    # Bf=$10, Bw=100/6.25=$16: $20 Fable + $2 haiku. Unclamped the pool would
    # see $22 > $16 and bill; clamped it sees 10+2=$12 -> only the sub-cap leg.
    over = _overage(day_fable_usd=20, day_other_usd=2, a_week=6.25, a_fable=10.0)
    assert set(over) == {"fable"}
    assert over["fable"] == pytest.approx(10.0)

def test_both_boundaries_never_bill_a_token_twice():
    # Bf=$10, Bw=$5, $20 Fable only. Plan covers 0..5; 5..10 is general
    # overage of pool-drawing Fable ($5 * clamp 10/20 share of the $20 comp
    # -> $5); beyond-cap 10..20 is the sub-cap leg ($10). Total $15 < $20.
    over = _overage(day_fable_usd=20, a_week=20.0, a_fable=10.0)
    assert over["fable"] == pytest.approx(15.0)

def test_no_mature_fit_no_boundary_no_billing():
    assert _overage(day_fable_usd=500) == {}


# ---- version+date price resolution ----------------------------------------------
def test_resolver_version_beats_family(toy_pricing):
    assert engine.resolve_rate("claude-opus-4-1", None) == (15.0, 75.0)
    assert engine.resolve_rate("claude-opus-4-8", None) == (5.0, 25.0)

def test_resolver_dated_period_then_open_rate(toy_pricing):
    from datetime import date
    assert engine.resolve_rate("claude-sonnet-5", date(2026, 7, 15)) == (2.0, 10.0)
    assert engine.resolve_rate("claude-sonnet-5", date(2026, 9, 1)) == (3.0, 15.0)

def test_standing_ruler_ignores_intro_pricing(toy_pricing):
    # THE guardrail: the subscription %-fit prices sonnet at the OPEN rate even
    # while intro pricing is live, so calibration history stays comparable.
    assert engine.price_for("claude-sonnet-5") == (3.0, 15.0)

def test_missing_pricing_degrades_to_hardcoded(no_pricing):
    assert engine.resolve_rate("claude-opus-4-1", None) == engine.PRICE["opus"]
    assert engine.resolve_rate("claude-unknownfamily-9", None) is None
