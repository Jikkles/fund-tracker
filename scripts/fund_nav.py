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

# FT's search API is the way in for funds Yahoo cannot find by name. It
# returns ISINs, and an ISIN then resolves on Yahoo reliably - name search is
# a lottery, an ISIN is an identifier. Server-rendered JSON, no key.
FT_SEARCH = "https://markets.ft.com/data/searchapi/searchsecurities?query={}"

SEARCH_URL = ("https://query2.finance.yahoo.com/v1/finance/search"
              "?q={}&quotesCount=8&newsCount=0")
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{}?interval=1d&range={}")

# Word-overlap floor for accepting a match, as a fraction of our own
# significant words, tolerating share-class and punctuation noise.
#
# Be clear about what this does NOT do, because the obvious reading is wrong.
# It does not separate "Artemis Income" from "Artemis Global Income": "income"
# is a stopword, so "artemis" is the only significant word left on our side
# and the pair scores a perfect 1.0. Nor does it separate the BlackRock funds
# that collided - they score 0.67, comfortably clear. What keeps those apart
# is the ISIN rung of the ladder and the collision rule in reject_collisions;
# this floor only screens out the plainly unrelated. fund_nav.py --selftest
# pins all three cases so a change here is a deliberate one.
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
    # ABBREV turns Yahoo's "APAC" into one word, so our own "Asia Pacific"
    # has to become the same one word or the two never meet - which is why
    # the APAC entry did nothing at all until this line existed.
    (r"\basia[\s-]+pacific\b", "asiapacific"),
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


def ft_isins(name: str) -> list[str]:
    """Candidate ISINs for a fund name, via FT's search API."""
    try:
        raw = _get(FT_SEARCH.format(urllib.parse.quote(name)))
        items = (json.loads(raw).get("data") or {}).get("security") or []
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError, AttributeError):
        return []
    out = []
    for item in items:
        sym = str(item.get("symbol") or "")
        code = sym.split(":")[0]
        # Accept only things shaped like an ISIN, and prefer the GBP lines -
        # a EUR or USD class is a different instrument.
        if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", code):
            label = str(item.get("name") or "")
            if overlap(name, label) >= 0.5:
                out.append((code, sym.endswith(":GBP") or sym.endswith(":GBX")))
    out.sort(key=lambda x: not x[1])          # GBP lines first
    return [c for c, _ in out]


def wanted_class(fund: dict) -> str | None:
    """The share-class letter this desk tracks, e.g. 'C' from 'Class C Acc'.

    One or two letters: houses issue "FD" and "ID" lines as well as "C" and
    "I", and a single-letter pattern reads a two-letter class as no class at
    all - which then compares equal to everything and waves a substitute
    through with no note.
    """
    m = re.search(r"\bclass\s+([A-Z]{1,2})\b",
                  str(fund.get("shareClass") or ""), re.I)
    return m.group(1).upper() if m else None


# Yahoo spells the accumulation marker both ways - "Artemis Global Income I
# Acc" but "Trojan Fund X Accumulation". A pattern anchored on the
# abbreviation alone reads the spelled-out word as no marker at all, which
# rejected sound candidates at search time and left "no accumulation class
# found" standing on funds whose own line says the word.
ACC = r"acc(?:um(?:ulation)?)?"


def class_verdict(label: str, want: str | None) -> tuple[bool, str]:
    """Judge a candidate's share class. (acceptable, note)."""
    # Income and distributing classes pay dividends away, so their price
    # series understates total return - never silently mix one in.
    if re.search(r"\b(inc|income|dist|distributing)\b", label, re.I) and \
       not re.search(rf"\b{ACC}\b", label, re.I):
        return False, "income/distributing class"
    if not re.search(rf"\b{ACC}\b", label, re.I):
        return False, "no accumulation class found"
    # The class letter stays case-sensitive: matching case-insensitively would
    # read ordinary capitalised words as class designators.
    got = re.search(r"\b([A-Z]{1,2})\s*(?:GBP\s*)?Acc(?:um(?:ulation)?)?\b", label)
    got = got.group(1).upper() if got else None
    if want and got and want != got:
        return True, f"share class {got} used, desk tracks {want}"
    return True, ""


def resolve(fund: dict) -> tuple[dict | None, str]:
    """Find a Yahoo symbol for one fund. Returns (match, reason).

    A ladder, strongest identifier first. Each rung still has to pass the same
    name and share-class checks - a weaker rung is not a licence to guess.
    """
    name = fund["name"]
    want = wanted_class(fund)

    def judge(hits, why):
        best = None
        for hit in hits:
            label = _label(hit)
            if not str(hit.get("symbol", "")).endswith(".L") and "isin" not in why:
                continue
            if overlap(name, label) < 0.5:
                continue
            ok, note = class_verdict(label, want)
            if not ok:
                continue
            rank = (0 if not note else 1)      # exact class beats a substitute
            if best is None or rank < best[0]:
                best = (rank, hit, f"{why}{' - ' + note if note else ''}")
        return best

    # 1. an ISIN we already hold
    for isin in filter(None, [fund.get("isin")]):
        got = judge(search(isin), f"isin {isin}")
        time.sleep(0.35)
        if got:
            return got[1], got[2]

    # 2. an ISIN sitting elsewhere in the record but never promoted to the
    #    isin field - three funds carried one in their prose
    blob = json.dumps(fund, ensure_ascii=False)
    for isin in dict.fromkeys(re.findall(r"\b(?:GB00|IE00|LU)[A-Z0-9]{7}\d\b", blob)):
        if isin == fund.get("isin"):
            continue
        got = judge(search(isin), f"isin {isin} (recovered)")
        time.sleep(0.35)
        if got:
            fund["isin"] = isin
            return got[1], got[2]

    # 3. the name, and its house abbreviation - Yahoo lists "L&G UK Index"
    #    and will not match it to a search for "Legal & General UK Index"
    for query in dict.fromkeys([name, brand_variant(name)]):
        if not query:
            continue
        got = judge(search(query), "name match")
        time.sleep(0.35)
        if got:
            return got[1], got[2]

    # 4. ask FT for an ISIN, then come back to Yahoo with it
    for isin in ft_isins(name)[:4]:
        got = judge(search(isin), f"isin {isin} (via FT)")
        time.sleep(0.35)
        if got:
            fund["isin"] = isin
            return got[1], got[2]

    return None, "no accumulation-class match on Yahoo, by name, ISIN or FT lookup"


def brand_variant(name: str) -> str | None:
    """Rewrite a house name the way Yahoo lists it."""
    swaps = [("Legal & General", "L&G"), ("JPMorgan", "JPM"),
             ("BNY Mellon", "BNY Mellon"), ("Janus Henderson", "Janus Henderson"),
             ("T. Rowe Price", "T. Rowe Price")]
    for full, short in swaps:
        if name.startswith(full) and short != full:
            return short + name[len(full):]
    return None


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
            "currency": meta.get("currency"),
            "name": meta.get("longName") or meta.get("shortName") or ""}


# A NAV series that jumps by more than this in one step is not reporting a
# return - it is a share-class redenomination or a data error. Invesco
# Tactical Bond priced at "-98.86% over 3yr" on exactly this.
MAX_DAILY_STEP = 0.5

# A series must reach back past the window's start, or the window is not
# really covered - a fund launched three months ago would otherwise report
# the same number for 1yr, 3yr and 5yr, a three-month return wearing a
# five-year label. The tolerance absorbs a weekend or holiday at the far end.
def _slack(days: int) -> int:
    return min(7, max(3, int(days * 0.05))) * 86400


def split_at_discontinuity(series: dict) -> tuple[dict, str]:
    """Drop everything up to the last redenomination.

    A 100:1 share-class change is not a -99% return, but nor does it spoil
    the data after it. Rather than refuse the fund outright, keep the clean
    tail; the window-coverage check then declines any period reaching back
    past the break, so nothing is reported that spans it.
    """
    closes, stamps = series["c"], series["t"]
    cut, note = 0, ""
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if not prev:
            continue
        if abs(cur - prev) / prev > MAX_DAILY_STEP:
            cut = i
            note = (f"series restarts {date.fromtimestamp(stamps[i]).isoformat()} "
                    f"after a {prev:g} -> {cur:g} share-class change")
    if not cut:
        return series, ""
    return {"t": stamps[cut:], "c": closes[cut:],
            "currency": series.get("currency")}, note


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
    if series["t"][0] > cutoff + _slack(days):
        return None
    return (series["c"][-1] - start) / start * 100.0


def pct_ytd(series: dict) -> float | None:
    """
    Calendar year-to-date total return, or None if the series cannot carry one.

    Anchored on the final close of LAST year, not the first close of this one,
    so a figure published on 2 January measures the move since the year turned
    rather than a single session against itself.

    Two refusals, both in the same spirit as `pct_over`'s window coverage:
    a fund whose history begins inside this year has no such anchor, and a
    series that has not priced at all this year has no year to date. Either
    one would otherwise publish a number wearing a label it has not earned.
    """
    if not series or len(series["t"]) < 2:
        return None
    year = date.today().year
    if date.fromtimestamp(series["t"][-1]).year < year:
        return None
    base = None
    for stamp, close in zip(series["t"], series["c"]):
        if date.fromtimestamp(stamp).year >= year:
            break
        base = close
    if not base:
        return None
    return (series["c"][-1] - base) / base * 100.0


def fmt(v: float | None) -> str | None:
    return None if v is None else f"{'+' if v >= 0 else ''}{v:.2f}%"


def reject_collisions(picks: dict[int, tuple[dict, str]],
                      funds: list[dict]) -> list[tuple[str, str]]:
    """
    Drop any symbol claimed by more than one fund. Mutates `picks`.

    "BlackRock Continental European" and "...European Income" both matched the
    same line. A collision is proof that at least one is wrong, and guessing
    which would be exactly the silent corruption this guards against - so
    neither keeps it, unless one got there by ISIN and the others did not.
    """
    refusals: list[tuple[str, str]] = []
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
    return refusals


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    only_resolve = "--resolve-only" in argv

    doc = json.loads(FUNDS.read_text(encoding="utf-8"))
    funds = doc["funds"]

    resolved = refused = priced = partial = 0
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

    refusals.extend(reject_collisions(picks, funds))

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
        # Derive the share-class caveat from the stored Yahoo name every run,
        # rather than keeping it as a by-product of the search. Once a symbol
        # is stored the ladder is skipped, so anything recorded only at search
        # time would silently disappear on the next run.
        _, class_note = class_verdict(label, wanted_class(fund))
        if class_note:
            fund["navClassNote"] = class_note
        else:
            fund.pop("navClassNote", None)

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

        # A symbol pinned by hand carries no name with it, so `label` was empty
        # and class_verdict judged an empty string - answering "no accumulation
        # class found" for 20 funds whose line is plainly an Acc class. That is
        # a caveat about the desk's own missing metadata, published as though
        # it were a fact about the fund. The chart names the line; adopt it and
        # judge the class on that.
        if not label and series.get("name"):
            label = series["name"]
            fund["navName"] = label
            _, class_note = class_verdict(label, wanted_class(fund))
            if class_note:
                fund["navClassNote"] = class_note
            else:
                fund.pop("navClassNote", None)

        series, split_note = split_at_discontinuity(series)
        if split_note:
            fund["navNote"] = split_note
        else:
            fund.pop("navNote", None)

        windows = {"nav1w": 7, "nav1m": 31, "nav1yr": 365,
                   "nav3yr": 365 * 3, "nav5yr": 365 * 5}
        vals = {k: pct_over(series, n) for k, n in windows.items()}
        # YTD is calendar-anchored, not a trailing window, so it is computed
        # separately rather than bolted onto `windows` with a day count that
        # would drift a little further from 1 January every day of the year.
        vals["navYtd"] = pct_ytd(series)
        one, five = vals["nav1yr"], vals["nav5yr"]
        # A history too short for a year still prices a week and a month
        # honestly, and those drive the 1W/1M rankings and the rolling
        # oneMonth figure. Refusing the whole fund because one window is
        # uncovered threw those away and left it publishing "not yet
        # verified" while a real NAV sat on the wire. Only a series that
        # covers no window at all has nothing to say.
        if all(v is None for v in vals.values()):
            print(f"  [nav] {fund['name'][:38]:40} {symbol:14} too short")
            continue

        if one is None:
            partial += 1
        else:
            priced += 1
        perf = fund.setdefault("performance", {})
        for key, value in vals.items():
            if value is None:
                perf.pop(key, None)
            else:
                perf[key] = fmt(value)
        perf["navAsAt"] = date.fromtimestamp(series["t"][-1]).isoformat()
        perf["navPoints"] = len(series["c"])
        print(f"  [nav] {fund['name'][:38]:40} {symbol:14} "
              f"1w {fmt(vals['nav1w']) or '  n/a':>8}  "
              f"1m {fmt(vals['nav1m']) or '  n/a':>8}  "
              f"ytd {fmt(vals['navYtd']) or '   n/a':>9}  "
              f"1yr {fmt(one) or '   n/a':>9}  5yr {fmt(five) or '   n/a':>9}")

    print(f"\n{resolved} resolved, {refused} refused, {priced} priced "
          f"of {len(funds)} funds"
          + (f" (+{partial} priced over short windows only - history too "
             f"short for a 1-year figure)" if partial else ""))
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


# ---------------------------------------------------------------------------
# Self-test - the guards, against captured payloads (runs offline)
# ---------------------------------------------------------------------------
# Resolution is the dangerous step on this desk. A wrong symbol does not fail
# loudly; it publishes confident, precise, wrong performance data, which is
# worse than an honestly stale figure. Every guard below exists because
# something got through once, and none of them had a test until now.
def _selftest_offline() -> bool:
    import sys as _sys

    def _series(closes, start=1_700_000_000, step=86400):
        return {"t": [start + i * step for i in range(len(closes))],
                "c": list(closes), "currency": "GBP"}

    # --- name matching ---------------------------------------------------
    # Yahoo truncates hard. These are real labels the resolver has to accept.
    assert overlap("Legal & General UK Index Trust", "L&G UK Index I Acc") == 1.0
    assert overlap("L&G Future World ESG Tilted & Optimised UK Index",
                   "L&G Fut Wld ESG Tilted & Optd UKIdxI Acc") >= 0.6
    assert overlap("", "anything") == 0.0
    # Share-class letters are stopwords: "Class I" must not lend a point of
    # similarity to every other fund with an I class.
    assert "i" not in words("L&G UK Index Class I Accumulation")
    assert "global" in words("Artemis Glbl Income"), "abbreviation not expanded"

    # Three things the floor does NOT do, pinned so a change to it is a
    # deliberate change rather than a surprise:
    #
    # 1. NAME_MATCH_MIN's own comment claims 0.6 "rejects 'Artemis Income'
    #    against 'Artemis Global Income'". It does not. "income" is a
    #    stopword, leaving "artemis" as the only significant word, so the
    #    pair scores a perfect 1.0. What actually separates those two funds
    #    is the ISIN rung and the collision rule below, not this floor.
    assert overlap("Artemis Income", "Artemis Global Income I Acc") == 1.0

    # 2. The BlackRock pair the collision rule exists for clears the floor
    #    comfortably. The floor is not what catches it.
    assert overlap("BlackRock Continental European",
                   "BlackRock European Dynamic D Acc") > NAME_MATCH_MIN

    # Yahoo's "APAC" and our "Asia Pacific" are normalised to the same single
    # word. Before the ALIASES entry existed the pair scored 0.5 and would
    # have been refused on a fresh resolve; Stewart Investors was carried by
    # its stored symbol alone.
    assert overlap("Stewart Investors Asia Pacific Leaders Sustainability",
                   "Stewart Inv APAC Ldrs B GBP Acc") >= NAME_MATCH_MIN
    assert "asiapacific" in words("Stewart Investors Asia Pacific Leaders")
    assert "asiapacific" in words("Stewart Inv APAC Ldrs B GBP Acc")
    # A lone "Pacific" is a different fund and must not be swept in.
    assert "asiapacific" not in words("iShares Pacific ex Japan Equity Index")
    print("  name overlap     OK  (2 real limits pinned, APAC fixed)",
          file=_sys.stderr)

    # --- share class -----------------------------------------------------
    assert wanted_class({"shareClass": "Class C Accumulation"}) == "C"
    assert wanted_class({"shareClass": "Accumulation"}) is None
    assert wanted_class({}) is None

    # Income classes pay dividends away, so their series understates total
    # return. They are never substituted, whatever else matches.
    assert class_verdict("Artemis Income I Inc", "I")[0] is False
    assert class_verdict("Jupiter UK Growth Distributing", None)[0] is False
    assert class_verdict("Some Fund GBP", None)[0] is False,         "a line with no accumulation marker must not be accepted"
    # An accumulation line in another class is allowed, but must say so - it
    # is the asterisk the page prints next to the figure. 30 funds carry one.
    ok, note = class_verdict("L&G UK Index I Acc", "C")
    assert ok and note == "share class I used, desk tracks C", (ok, note)
    # The exact class earns no note, and so no asterisk.
    ok, note = class_verdict("L&G UK Index I Acc", "I")
    assert ok and note == "", (ok, note)

    # Two-letter classes. Houses issue "FD" and "ID" lines as well as "C" and
    # "I", and a single-letter pattern read those as no class at all - which
    # compares equal to everything and waved a substitute through unmarked.
    ok, note = class_verdict("BlackRock European Dynamic FD Acc", "D")
    assert ok and note == "share class FD used, desk tracks D", (ok, note)
    assert wanted_class({"shareClass": "Class FD Accumulation"}) == "FD"
    assert wanted_class({"shareClass": "Class ID Accumulation"}) == "ID"
    # An exact two-letter match still earns no note, and so no asterisk.
    ok, note = class_verdict("Example FD Acc", "FD")
    assert ok and note == "", (ok, note)
    # Single-letter classes are untouched by the widening.
    assert wanted_class({"shareClass": "Class C Accumulation"}) == "C"
    # An empty label is not evidence of anything, but class_verdict cannot say
    # so - it can only report what it sees. These are the real Yahoo names of
    # two funds that carried "no accumulation class found" for months purely
    # because the desk had never stored a name to judge.
    assert class_verdict("Schroder US Smaller Companies L Acc", "L") == (True, "")
    assert class_verdict("BlackRock European Dynamic D Acc", "D") == (True, "")
    assert class_verdict("", "L") == (False, "no accumulation class found")
    # Yahoo spells the marker out as often as it abbreviates. Trojan Fund
    # carried "no accumulation class found" against a line whose own name is
    # "Trojan Fund X Accumulation".
    assert class_verdict("Trojan Fund X Accumulation", "X") == (True, "")
    assert class_verdict("Fidelity Index US P Accumulation", "P") == (True, "")
    assert class_verdict("Some Fund Accum", None)[0] is True
    ok, note = class_verdict("Trojan Fund O Accumulation", "X")
    assert ok and note == "share class O used, desk tracks X", note
    # The wider spelling must not smuggle an income line through.
    assert class_verdict("Trojan Fund X Income", "X")[0] is False
    print("  share class      OK  (income refused, substitute flagged, "
          "empty label pinned)", file=_sys.stderr)

    # --- redenomination --------------------------------------------------
    # The guard that caught Invesco Tactical Bond and Ninety One Diversified
    # Income. A 100:1 share-class change is not a -99% return; without this
    # the desk published "-98.86% over 3yr" as fact.
    clean, note = split_at_discontinuity(_series([100.0] * 40))
    assert note == "" and len(clean["c"]) == 40, "steady series must be untouched"

    split = _series([100.0] * 20 + [1.0] * 20)
    kept, note = split_at_discontinuity(split)
    assert note and "share-class change" in note, note
    assert kept["c"] == [1.0] * 20, "must keep the clean tail, not the whole"
    assert len(kept["t"]) == len(kept["c"])

    # A real move, however violent, is a return and must survive. 2020 exists.
    kept, note = split_at_discontinuity(_series([100.0, 70.0, 90.0]))
    assert note == "", f"a -30% day is a market, not a redenomination: {note}"
    print(f"  redenomination   OK  (100:1 split cut, -30% day kept)",
          file=_sys.stderr)

    # --- window coverage -------------------------------------------------
    # A fund with three months of history must not report a 1-year figure.
    # Without this it reports the same number for 1yr, 3yr and 5yr - a
    # three-month return wearing a five-year label.
    short = _series([100.0 + i for i in range(90)])
    assert pct_over(short, 30) is not None, "30d is covered by 90d of history"
    assert pct_over(short, 365) is None, "1yr must be refused on 90d of data"
    assert pct_over(short, 1825) is None

    long = _series([100.0] * 400 + [110.0])
    got = pct_over(long, 365)
    assert got is not None and abs(got - 10.0) < 0.5, got
    assert pct_over(_series([100.0]), 30) is None, "one point is not a series"
    assert pct_over({"t": [], "c": []}, 30) is None
    # A zero start cannot be divided by.
    assert pct_over(_series([0.0] + [1.0] * 40), 30) is not None
    print("  window coverage  OK  (short history refused a long label)",
          file=_sys.stderr)

    # --- year to date ----------------------------------------------------
    # YTD is anchored on last year's final close, so the baseline is the year
    # boundary rather than whichever session happened to open the year.
    import datetime as _dt
    _today = date.today()
    _jan1 = _dt.datetime(_today.year, 1, 1)
    _days_in = (_today - _jan1.date()).days

    def _spanning(pre, post):
        """Closes running up to 31 Dec, then `post` continuing into this year."""
        start = int((_jan1 - _dt.timedelta(days=len(pre))).timestamp())
        return _series(pre + post, start=start)

    # 100.0 through the end of last year, then a step to 110.0 this year.
    pre = [100.0] * 40
    post = [110.0] * max(1, min(_days_in, 5))
    ytd = pct_ytd(_spanning(pre, post))
    assert ytd is not None and abs(ytd - 10.0) < 1e-9, ytd

    # A fund whose whole history starts inside this year has no anchor in
    # last year, so it gets nothing rather than an inception return labelled
    # "YTD" - the same refusal pct_over makes for an uncovered window.
    this_year_only = _series([100.0, 110.0],
                             start=int((_jan1 + _dt.timedelta(days=1)).timestamp()))
    assert pct_ytd(this_year_only) is None, "no pre-Jan-1 anchor must refuse"

    # A series that stopped last year has no year to date at all.
    stale = _series([100.0] * 40,
                    start=int((_jan1 - _dt.timedelta(days=60)).timestamp()))
    assert pct_ytd(stale) is None, "a series with nothing this year must refuse"

    assert pct_ytd(_series([100.0])) is None, "one point is not a series"
    assert pct_ytd({"t": [], "c": []}) is None
    print("  year to date     OK  (anchored on 31 Dec, no-anchor refused)",
          file=_sys.stderr)

    # --- collisions ------------------------------------------------------
    funds = [{"name": "BlackRock Continental European"},
             {"name": "BlackRock Continental European Income"},
             {"name": "Artemis Income"}]
    picks = {0: ({"symbol": "0PSAME.L"}, "name match"),
             1: ({"symbol": "0PSAME.L"}, "name match"),
             2: ({"symbol": "0POTHER.L"}, "name match")}
    refused = reject_collisions(picks, funds)
    assert len(refused) == 2, refused
    assert 0 not in picks and 1 not in picks, "neither may keep a tie"
    assert 2 in picks, "an uncontested symbol must survive"

    # Unless exactly one got there by ISIN, which is real evidence.
    picks = {0: ({"symbol": "0PSAME.L"}, "isin GB00TEST1234"),
             1: ({"symbol": "0PSAME.L"}, "name match")}
    refused = reject_collisions(picks, [funds[0], funds[1]])
    assert list(picks) == [0], picks
    assert len(refused) == 1 and "collision" in refused[0][1]
    print("  collisions       OK  (tie refused, ISIN claimant kept)",
          file=_sys.stderr)

    # --- formatting ------------------------------------------------------
    assert fmt(None) is None
    assert fmt(0) == "+0.00%"
    assert fmt(-3.456) == "-3.46%"
    assert fmt(12.0) == "+12.00%"

    print("  fund_nav self-test: OK", file=_sys.stderr)
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest_offline()
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
