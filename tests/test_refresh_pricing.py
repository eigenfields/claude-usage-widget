"""Pins for the pricing-refresh merge contract — written after the live
2026-07-13 incident where the page's active Sonnet 5 intro price ($2/$10)
would have overwritten the standing 3/15 rate (the calibration ruler) had the
PR merged. The rule: while a dated period is active, the page shows THAT
price, so it carries zero information about the standing rate.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import refresh_pricing as rp  # noqa: E402


def doc_with_sonnet_promo():
    return {
        "models": {
            "sonnet-5": [{"until": "2026-08-31", "input": 2, "output": 10},
                         {"input": 3, "output": 15}],
            "opus-4-8": {"input": 5, "output": 25},
        },
        "family_fallback": {"sonnet": "sonnet-5", "opus": "opus-4-8"},
    }


def open_rate(doc, key):
    e = rp.open_entry(doc["models"][key])
    return (float(e["input"]), float(e["output"]))


def test_active_promo_never_touches_the_standing_rate():
    doc = doc_with_sonnet_promo()
    # the page shows the intro price while the promo is live — the exact
    # payload the maiden run tried to write into the open entry
    rp.merge_rates(doc, {"sonnet-5": (2.0, 10.0)}, today=date(2026, 7, 13))
    assert open_rate(doc, "sonnet-5") == (3.0, 15.0)


def test_odd_price_during_promo_is_left_for_humans():
    doc = doc_with_sonnet_promo()
    rp.merge_rates(doc, {"sonnet-5": (2.5, 12.0)}, today=date(2026, 7, 13))
    assert open_rate(doc, "sonnet-5") == (3.0, 15.0)
    assert doc["models"]["sonnet-5"][0] == {"until": "2026-08-31",
                                            "input": 2, "output": 10}


def test_after_promo_expiry_the_open_entry_updates_again():
    doc = doc_with_sonnet_promo()
    rp.merge_rates(doc, {"sonnet-5": (4.0, 20.0)}, today=date(2026, 9, 1))
    assert open_rate(doc, "sonnet-5") == (4.0, 20.0)


def test_undated_model_updates_and_new_model_is_not_added():
    doc = doc_with_sonnet_promo()
    rp.merge_rates(doc, {"opus-4-8": (6.0, 30.0), "nova-1": (1.0, 5.0)},
                   today=date(2026, 7, 13))
    assert open_rate(doc, "opus-4-8") == (6.0, 30.0)
    assert "nova-1" not in doc["models"]          # humans add models, not CI


def test_parse_rates_reads_pricing_card_text():
    text = ("Claude Sonnet 5   Input $2 / MTok   Output $10 / MTok   "
            "Claude Opus 4.8   Input $5 per MTok   Output $25 per MTok")
    rates = rp.parse_rates(text)
    assert rates["sonnet-5"] == (2.0, 10.0)
    assert rates["opus-4-8"] == (5.0, 25.0)


def test_sanity_gate_catches_the_bad_shapes():
    doc = doc_with_sonnet_promo()
    assert rp.sane(doc) == []
    doc["models"]["opus-4-8"]["output"] = 2       # output < input
    assert any("output < input" in e for e in rp.sane(doc))
    doc["family_fallback"]["haiku"] = "haiku-4-5" # anchor points at nothing
    assert any("anchor missing" in e for e in rp.sane(doc))
