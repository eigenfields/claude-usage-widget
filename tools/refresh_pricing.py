"""Refresh pricing.json from https://claude.com/pricing#api — CI-only.

Runs on GitHub's infra (see .github/workflows/pricing-refresh.yml) and lands
as a REVIEWED pull request. It is never executed on an end user's machine and
the widget itself never calls the network.

Merge contract:
  - only the OPEN (undated) rate of a model it confidently re-found is updated;
  - dated periods (e.g. Sonnet 5 intro pricing) and _notes are preserved;
  - models it can't find in the page are left untouched;
  - _updated bumps only when something actually changed.

Sanity gate (marketing HTML WILL change shape eventually): every
family_fallback anchor must be present in pricing.json after the merge, every
rate must sit in a plausible band, and output >= input everywhere. Any
violation -> non-zero exit, nothing written, no PR. A broken parse must never
silently ship.
"""
import json, re, sys, urllib.request
from datetime import date, timezone, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICING = ROOT / "pricing.json"
URL = "https://claude.com/pricing"

# model display-names to look for on the page -> pricing.json keys
TARGETS = {
    "fable 5": "fable-5",
    "mythos 5": "mythos-5",
    "opus 4.8": "opus-4-8",
    "opus 4.7": "opus-4-7",
    "opus 4.6": "opus-4-6",
    "opus 4.5": "opus-4-5",
    "opus 4.1": "opus-4-1",
    "sonnet 5": "sonnet-5",
    "sonnet 4.6": "sonnet-4-6",
    "sonnet 4.5": "sonnet-4-5",
    "haiku 4.5": "haiku-4-5",
}
BAND = (0.1, 200.0)          # plausible $/MTok range


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pricing-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", html)


def parse_rates(text):
    """For each target name, take the first two $-figures within reach of it —
    input then output, the order every pricing card uses."""
    found = {}
    low = text.lower()
    for name, key in TARGETS.items():
        m = re.search(re.escape(name), low)
        if not m:
            continue
        window = text[m.end():m.end() + 400]
        prices = re.findall(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)?\s*mtok",
                            window, flags=re.I)
        if len(prices) >= 2:
            found[key] = (float(prices[0]), float(prices[1]))
    return found


def open_entry(val):
    """The open (undated) entry of a models value, whichever shape it has."""
    if isinstance(val, list):
        for e in val:
            if "until" not in e:
                return e
        return None
    return val


def sane(doc):
    errs = []
    models = doc.get("models", {})
    for fam, anchor in doc.get("family_fallback", {}).items():
        if anchor not in models:
            errs.append(f"family_fallback anchor missing: {fam} -> {anchor}")
    for key, val in models.items():
        for e in (val if isinstance(val, list) else [val]):
            try:
                i, o = float(e["input"]), float(e["output"])
            except (KeyError, TypeError, ValueError):
                errs.append(f"{key}: malformed entry {e}")
                continue
            if not (BAND[0] <= i <= BAND[1] and BAND[0] <= o <= BAND[1]):
                errs.append(f"{key}: rate outside {BAND}: in={i} out={o}")
            if o < i:
                errs.append(f"{key}: output < input ({o} < {i})")
    return errs


def main():
    doc = json.loads(PRICING.read_text(encoding="utf-8"))
    before = json.dumps(doc, sort_keys=True)

    text = fetch_text(URL)
    rates = parse_rates(text)
    if not rates:
        print("FAIL: page yielded zero recognizable model rates — layout changed?")
        return 1
    print(f"parsed {len(rates)} model rates from {URL}")

    for key, (pin, pout) in sorted(rates.items()):
        cur = doc["models"].get(key)
        if cur is None:
            print(f"  NEW model on page (left for human review, not added): {key} {pin}/{pout}")
            continue
        e = open_entry(cur)
        if e is None:
            print(f"  {key}: no open entry (dated-only) — skipped")
            continue
        if (float(e["input"]), float(e["output"])) != (pin, pout):
            print(f"  {key}: {e['input']}/{e['output']} -> {pin}/{pout}")
            e["input"], e["output"] = pin, pout
        else:
            print(f"  {key}: unchanged {pin}/{pout}")

    errs = sane(doc)
    if errs:
        print("FAIL: sanity gate:")
        for x in errs:
            print("  -", x)
        return 1

    if json.dumps(doc, sort_keys=True) == before:
        print("no changes — nothing to write")
        return 0
    doc["_updated"] = datetime.now(timezone.utc).date().isoformat()
    PRICING.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"pricing.json updated ({doc['_updated']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
