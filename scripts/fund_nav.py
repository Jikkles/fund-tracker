"""
Price the funds themselves, from published NAVs.

The desk was built on the premise that no free source publishes active fund
NAVs, so 46 of 63 funds carried "not yet verified" and their headline figures
aged indefinitely. That premise turns out to be wrong: Yahoo carries UK OEICs
under Morningstar-style symbols (0P0000W36K.L), in GBP, priced daily, and its
existing chart endpoint serves them exactly like any equity.

This module resolves each fund to such a symbol and prices it.

RESOLUTION IS THE RISKY PART, not the pricing. A wrong symbol produces
confident, precise, wrong performance data - strictly worse than an honestly
stale figure. So matching is deliberately conservative:

  * An ISIN search is trusted only if the returned fund name also shares
    enough words with ours. A mistyped ISIN that happens to resolve is
    otherwise indistinguishable from a good match.
  * A name search must return a London-listed (.L) GBP line whose name
    overlaps ours strongly, and accumulation classes are preferred because
    that is what this desk tracks. Income classes understate total return,
    so picking one silently would corrupt the comparison.
  * Anything that does not clear those bars is REFUSED and reported. The
    fund keeps whatever it had and stays marked unverified.

Resolved symbols are written back to the fund record, so the search only
happens once per fund and every later run is a straight price lookup that a
human can audit against the stored name.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "fund-tracker/1.0 (+github actions; personal research desk)")
TIMEOUT = 25
FUNDS = Path(__file__).resolve().parent.parent / "data" / "funds.json"

SEARCH_URL = ("https://query2.finance.yahoo.com/v1/finance/search"
              "?q={}&quotesCount=8&newsCount=0")
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{}?interval=1d&range={}")

# Word-overlap floor for accepting a match, as a fraction of our own
# significant words. 0.6 rejects "Artemis Income" against "Artemis Global
# Income" while still tolerating share-class and punctuation noise.
NAME_MATCH_MIN = 0.6

# Words that carry no identifying information and would inflate the overlap
# score. Share-class tokens are stripped separately.
STOPWORDS = {
    "fund", "trust", "the", "and", "of", "class", "acc", "inc", "accumulation",
    "income", "gbp", "hedged", "unhedged", "ltd", "plc", "oeic", "icvc",
    "sterling", "a", "b", "c", "d", "i", "j", "l", "p", "r", "s", "w", "x", "z",
}

# Yahoo truncates fund names hard ("L&G Fut Wld ESG Tilted & Optd UKIdxI£Acc",
# "Stewart Inv APAC Ldrs B GBP Acc"). Without expanding these, word overlap
# refuses funds that are plainly the right ones.
ABBREV = {
    "glbl": "global", "gbl": "global", "fut": "future", "wld": "world",
    "optd": "optimised", "opt": "optimised", "ldrs": "leaders",
    "eq": "equity", "eqty": "equity", "idx": "index", "mkts": "markets",
    "mkt": "market", "inv": "investors", "apac": "asiapacific",
    "eurp": "european", "eur": "european", "jpn": "japan", "jap": "japan",
    "sits": "situations", "opps": "opportunities", "cont": "continental",
    "corp": "corporate", "govt": "government", "intl": "international",
    "amer": "american", "emg": "emerging", "emerg": "emerging",
    "bal": "balanced", "divers": "diversified", "sust": "sustainable",
    "respons": "responsible", "strat": "strategic", "sml": "smaller",
    "smlr": "smaller", "cos": "companies", "co": "companies",
    "tr": "trust", "instl": "institutional", "inst": "institutional",
    "ma": "multiasset", "multi": "multiasset", "asset": "",
    "uk": "uk", "us": "us",
}

# Normalisations so "L&G" matches "Legal & General" and similar.
ALIASES = [
    (r"\bl&g\b", "legal general"),
    (r"\bl and g\b", "legal general"),
    (r"&", " "),
    (r"\bjpm\b", "jpmorgan"),
    (r"\bbny\b", "bny mellon"),
    (r"\bft f\b", "ftf"),
    (r"\bst\.? james'?s\b", "st jamess"),
    (r"\bt\.? rowe\b", "t rowe"),
]


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def words(name: str) -> set[str]:
    """Significant lowercase words in a fund name."""
    s = name.lower()
    for pattern, repl in ALIASES:
        s = re.sub(pattern, repl, s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    # Split runs like "ukidxi" that Yahoo jams together without spaces.
    out = set()
    for w in s.split():
        w = ABBREV.get(w, w)
        if w and w not in STOPWORDS and len(w) > 1:
            out.add(w)
    return out


def overlap(ours: str, theirs: str) -> float:
    """Fraction of our significant words that appear in theirs."""
    a, b = words(ours), words(theirs)
    if not a:
        return 0.0
    return len(a & b) / len(a)


def search(query: str) -> list[dict]:
    try:
        raw = _get(SEARCH_URL.format(urllib.parse.quote(query)))
        payload = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError):
        return []
    return [q for q in payload.get("quotes", [])
            if q.get("quoteType") == "MUTUALFUND"]


def _label(quote: dict) -> str:
    return str(quote.get("longname") or quote.get("shortname") or "")


def resolve(fund: dict) -> tuple[dict | None, str]:
    """Find a Yahoo symbol for one fund. Returns (match, reason)."""
    name = fund["name"]
    isin = fund.get("isin")

    if isin:
        hits = search(isin)
        time.sleep(0.4)
        for hit in hits:
            score = overlap(name, _label(hit))
            # An ISIN is strong evidence, but a typo'd ISIN also resolves to
            # *something* - the name has to corroborate it.
            if score >= 0.5:
                return hit, f"isin {isin} (name {score:.0%})"
        if hits:
            return None, (f"isin {isin} resolved to {_label(hits[0])!r}, which "
                          f"does not match the fund name - refusing")

    hits = search(name)
    time.sleep(0.4)

    # London-listed GBP lines only. A USD or NOK class is a different
    # instrument and would silently import currency return.
    candidates = []
    for hit in hits:
        sym = hit.get("symbol", "")
        if not sym.endswith(".L"):
            continue
        label = _label(hit)
        score = overlap(name, label)
        if score < NAME_MATCH_MIN:
            continue
        # This desk tracks accumulation classes; income classes pay dividends
        # out and would understate total return against the rest of the list.
        is_acc = bool(re.search(r"\bacc\b", label, re.I))
        candidates.append((is_acc, score, hit, label))

    if not candidates:
        return None, "no London-listed GBP match above the name threshold"

    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    is_acc, score, hit, label = candidates[0]
    if not is_acc:
        return None, (f"only non-accumulation classes found "
                      f"(best: {label!r}) - refusing rather than mixing bases")
    return hit, f"name match {score:.0%}"


def price(symbol: str, span: str = "5y") -> dict | None:
    """Daily NAV series for a symbol."""
    try:
        raw = _get(CHART_URL.format(urllib.parse.quote(symbol), span))
        result = json.loads(raw)["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        stamps = result["timestamp"]
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
            KeyError, IndexError, TypeError):
        return None

    pairs = [(int(t), float(c)) for t, c in zip(stamps, closes)
             if t is not None and c is not None]
    if len(pairs) < 30:
        return None
    meta = result.get("meta") or {}
    return {"t": [p[0] for p in pairs], "c": [p[1] for p in pairs],
            "currency": meta.get("currency")}


# A NAV series that jumps by more than this in one step is not reporting a
# return - it is a share-class redenomination or a data error. Invesco
# Tactical Bond priced at "-98.86% over 3yr" on exactly this.
MAX_DAILY_STEP = 0.5

# How much of a requested window the series must actually cover. Without
# this, a fund launched three months ago reports the same number for 1yr,
# 3yr and 5yr - a three-month return wearing a five-year label.
MIN_WINDOW_COVER = 0.9


def has_discontinuity(series: dict) -> tuple[bool, str]:
    """True if the series contains a jump no real fund return explains."""
    closes = series["c"]
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if not prev:
            continue
        step = abs(cur - prev) / prev
        if step > MAX_DAILY_STEP:
            when = date.fromtimestamp(series["t"][i]).isoformat()
            return True, (f"{step * 100:.0f}% single-step move on {when} "
                          f"({prev:g} -> {cur:g}) - looks like a share-class "
                          f"change, not a return")
    return False, ""


def pct_over(series: dict, days: int) -> float | None:
    """Total return over a trailing window, or None if not truly covered."""
    if not series or len(series["t"]) < 2:
        return None
    end = series["t"][-1]
    cutoff = end - days * 86400
    start = None
    for stamp, close in zip(series["t"], series["c"]):
        if stamp >= cutoff:
            start, start_t = close, stamp
            break
    if start is None or not start:
        return None
    # Refuse to label a short history with a long window.
    if (end - start_t) < days * 86400 * MIN_WINDOW_COVER:
        return None
    return (series["c"][-1] - start) / start * 100.0


def fmt(v: float | None) -> str | None:
    return None if v is None else f"{'+' if v >= 0 else ''}{v:.2f}%"


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    only_resolve = "--resolve-only" in argv

    doc = json.loads(FUNDS.read_text(encoding="utf-8"))
    funds = doc["funds"]

    resolved = refused = priced = 0
    refusals: list[tuple[str, str]] = []
    today = date.today()

    # Pass 1: resolve every fund, then reject any symbol claimed by more than
    # one fund. "BlackRock Continental European" and "...European Income" both
    # matched the same line - a collision is proof at least one is wrong, and
    # guessing which would be exactly the silent corruption this guards against.
    picks: dict[int, tuple[dict, str]] = {}
    for i, fund in enumerate(funds):
        stored = fund.get("navSymbol")
        if stored:
            picks[i] = ({"symbol": stored, "longname": fund.get("navName", "")},
                        "stored")
        else:
            match, reason = resolve(fund)
            if match:
                picks[i] = (match, reason)
            else:
                refusals.append((fund["name"], reason))

    claimed: dict[str, list[int]] = {}
    for i, (match, _) in picks.items():
        claimed.setdefault(match["symbol"], []).append(i)
    for symbol, owners in claimed.items():
        if len(owners) < 2:
            continue
        # An ISIN-sourced match is stronger evidence than a name match, so if
        # exactly one claimant got there by ISIN, it keeps the symbol.
        by_isin = [i for i in owners if picks[i][1].startswith("isin")]
        keep = by_isin[0] if len(by_isin) == 1 else None
        for i in owners:
            if i == keep:
                continue
            other = ", ".join(funds[j]["name"] for j in owners if j != i)
            refusals.append((funds[i]["name"],
                             f"symbol {symbol} also matched by {other} - "
                             f"collision, refusing"))
            picks.pop(i)

    for i, fund in enumerate(funds):
        if i not in picks:
            refused += 1
            continue
        match, reason = picks[i]
        resolved += 1
        symbol = match["symbol"]
        label = _label(match) or fund.get("navName", "")
        fund["navSymbol"] = symbol
        fund["navName"] = label
        fund["navMatch"] = reason

        if only_resolve:
            print(f"  [nav] {fund['name'][:38]:40} {symbol:14} {reason:22} "
                  f"{label[:40]}")
            continue

        series = price(symbol)
        time.sleep(0.3)
        if not series:
            refusals.append((fund["name"], f"{symbol} would not price"))
            print(f"  [nav] {fund['name'][:38]:40} {symbol:14} NO PRICE DATA")
            continue

        broken, why = has_discontinuity(series)
        if broken:
            refusals.append((fund["name"], f"{symbol}: {why}"))
            print(f"  [nav] {fund['name'][:38]:40} {symbol:14} REFUSED  {why}")
            continue

        one = pct_over(series, 365)
        three = pct_over(series, 365 * 3)
        five = pct_over(series, 365 * 5)
        if one is None:
            print(f"  [nav] {fund['name'][:38]:40} {symbol:14} too short")
            continue

        priced += 1
        perf = fund.setdefault("performance", {})
        perf["nav1yr"] = fmt(one)
        if three is not None:
            perf["nav3yr"] = fmt(three)
        else:
            perf.pop("nav3yr", None)
        if five is not None:
            perf["nav5yr"] = fmt(five)
        else:
            perf.pop("nav5yr", None)
        perf["navAsAt"] = date.fromtimestamp(series["t"][-1]).isoformat()
        perf["navPoints"] = len(series["c"])
        print(f"  [nav] {fund['name'][:38]:40} {symbol:14} "
              f"1yr {fmt(one):>9}  3yr {fmt(three) or '   n/a':>9}  "
              f"5yr {fmt(five) or '   n/a':>9}")

    print(f"\n{resolved} resolved, {refused} refused, {priced} priced "
          f"of {len(funds)} funds")
    if refusals:
        print("\nNeeds a hand-checked symbol in `navSymbol` "
              "(left unverified for now):")
        for name, reason in refusals:
            print(f"  - {name}: {reason}")

    if dry:
        print("\n[dry-run] nothing written")
        return 0

    doc.setdefault("meta", {})["navRefreshed"] = today.isoformat()
    FUNDS.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                     encoding="utf-8")
    print(f"\nwrote {FUNDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
