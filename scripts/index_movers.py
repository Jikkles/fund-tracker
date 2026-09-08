"""
Top and bottom movers among the CONSTITUENTS of each market index.

WHY ONLY SOME INDICES
---------------------
"Top 3 in the FTSE 100" is only true if all 100 were priced. Ranking 30 of
them and calling the best three a top three is exactly the kind of confident,
unfounded claim this desk exists not to make. So an index appears here only
where its full constituent list can be obtained and priced:

  FTSE 100     HL publish the whole index with prices - one page, and it is
  FTSE 250     the same platform the desk buys through. The 250 is paginated
               110 rows at a time, so it takes three requests rather than one.
  S&P 500      constituent list from Wikipedia, prices from Yahoo.
  Dow Jones    same, and only 30 names.
  Hang Seng    same. Its tickers are SEHK board lot numbers, which map onto
               Yahoo symbols by zero-padding to four digits and adding .HK -
               one exchange, one mechanical rule, no guesswork.
  Euro STOXX   same. This one was left out for years on the grounds that its
  50           constituents span eight exchanges and mapping their tickers
               would be error-prone - but the article's ticker column already
               carries the exchange suffix (ADS.DE, ADYEN.AS, NDA-FI.HE), so
               nothing is mapped at all. All 50 price, and every name Yahoo
               returns matches the name beside it in the list.
  Nikkei 225   same, from the Components list rather than a table: each entry
               carries its Tokyo code as "(TYO: 7203)", which is 7203.T. The
               list runs to 223 of the 225, so the panel says 223 of 225.

  Nasdaq 100   same. The desk charted the Nasdaq COMPOSITE until the panel
               went in beside it, and the Composite is the one index that
               cannot be ranked: ~3,000 names is not a load to put on a free
               price endpoint, and ranking the 100 while the tab said
               Composite would have been a different index under this one's
               name. Charting the 100 instead settles it honestly - the same
               market read through the names that drive it, all of which price.

Which is every equity index the desk charts, so the only entries below are
the lines that are not indices at all.

Deliberately absent, with the reason recorded in the output so the page can
say it rather than showing an empty panel:

  Gold, Brent,       not indices. They have no constituents, and saying so is
  10yr, GBP/USD      the correct output rather than an empty list.

WHY THE DISPLAYED NAME COMES FROM THE PRICE SOURCE
--------------------------------------------------
Wikipedia supplies the list of symbols; Yahoo supplies both the price and the
company name, read out of the same response. So the name beside a figure is
whatever the endpoint that produced the figure calls it, and the two cannot
disagree - the mis-mapping risk above stays a risk of pricing the wrong
company, never of labelling the right price with the wrong name.

HOW THE INDEX IS PRICED, AND WHY IT IS TWO PASSES
-------------------------------------------------
It used to be one request per constituent: 993 of them every daily run, 83%
of this desk's entire request load, to publish six rows per index and throw
the other ~987 prices away. Yahoo's spark endpoint takes fifty symbols at a
time, so the sweep is now twenty requests instead of a thousand.

Spark does not return company names, which is what the paragraph above is
about. So the ends of the ranking - and only the ends, because the middle is
never shown - are read a second time through the per-symbol chart endpoint,
which returns Yahoo's own name alongside its own price. That is ~18 requests
per index, and it makes the guarantee stronger rather than weaker: every
published row is now priced twice from two different endpoints, and a row
whose two readings disagree by more than CONFIRM_TOLERANCE is dropped
instead of shown.

WHAT ELSE THE SWEEP PAYS FOR
----------------------------
Having priced every constituent, the run also records BREADTH - how many rose,
how many fell, and the median move - which costs nothing and answers a
question the top and bottom three cannot. A day where 62 of 100 names rose is
a different market from one where 12 did, and both can produce the same top
three.

The constituent lists change rarely, so they are cached in data/ and only
refetched when older than CONSTITUENT_MAX_AGE. Prices are always fresh.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

import netfetch

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "movers.json"
CACHE = ROOT / "data" / "constituents.json"

USER_AGENT = netfetch.UA_BROWSER
WORKERS = 6                 # polite concurrency against a free endpoint
CONSTITUENT_MAX_AGE = 7     # days before a cached constituent list is refetched
SHOWN = 3                   # rows per list; the panel sits beside the chart
PRICED_FLOOR = 0.9          # of the constituent list, below which it is a guess

# ---------------------------------------------------------------------------
# Batch pricing
# ---------------------------------------------------------------------------
# This module used to ask Yahoo's chart endpoint for ONE SYMBOL AT A TIME:
# 993 requests every daily run, across the six Wikipedia-sourced indices, to
# publish six rows each and discard the other 957 prices. It was 83% of the
# desk's entire daily request load and the single least defensible thing the
# automation did to a free endpoint.
#
# Yahoo's spark endpoint takes a comma-separated list and returns a close
# series per symbol - the same closes, in batches. 993 requests becomes 20.
#
# What spark does NOT return is a company name, and the name is load-bearing
# here: the docstring above promises that the label beside a figure comes from
# the same response as the figure, so a mis-mapped ticker can only ever price
# the wrong company, never mislabel the right price. That promise is kept by
# confirming ONLY THE ROWS ACTUALLY PUBLISHED - six per index - with the
# per-symbol chart call this module already had. The bulk sweep chooses which
# names to show; the confirmation says what they are and re-reads the figure.
#
# So the guarantee is strictly stronger than before: every published row is
# now priced twice, from two different endpoints, and a row whose two readings
# disagree is dropped rather than shown.
SPARK_URL = ("https://query1.finance.yahoo.com/v8/finance/spark"
             "?symbols={}&range=5d&interval=1d")
SPARK_BATCH = 50            # symbols per request; keeps the URL well short of 2KB

# How far the confirmation read may differ from the sweep before the row is
# refused. Both are last-close-against-previous-close from Yahoo, minutes
# apart, so any real disagreement means they are not describing the same
# thing and neither can be trusted.
CONFIRM_TOLERANCE = 0.75    # percentage points

# Indices the desk charts but deliberately does not rank, and why. The page
# prints these, so an absent panel explains itself instead of looking broken.
UNSUPPORTED = {
    "gold-usd-oz": "Not an index - a single commodity price, so it has no "
                   "constituents to rank.",
    "brent-crude": "Not an index - a single commodity price.",
    "us-10yr-yield": "Not an index - a single government bond yield.",
    "gbp-usd": "Not an index - a single exchange rate.",
}


def get(url: str, tries: int = 3) -> str | None:
    """A page, or None. Retries, backoff and rate limiting live in netfetch.

    What was here retried three times with NO PAUSE between attempts and
    swallowed every exception identically - so a 429 was answered with two
    more requests inside a few milliseconds, which is the one reply certain
    to extend a throttle rather than clear it.
    """
    r = netfetch.fetch(url, ua=USER_AGENT, tries=tries, headers={
        "Accept": "text/html,application/json",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return r.text if r else None


def text_of(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def cells_of(row: str) -> list[str]:
    return [text_of(c) for c in
            re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]


def rows_of(page: str) -> list[str]:
    return re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I)


def tables_of(page: str) -> list[str]:
    return re.findall(r"<table[^>]*>(.*?)</table>", page, re.S | re.I)


def norm(s: str) -> str:
    """Header text down to something comparable - Wikipedia writes 'GICS  Sector'."""
    return re.sub(r"\s+", " ", s or "").strip().lower()


def pct(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return float(m.group(0)) if m else None


# ------------------------------------------------------------ HL summaries

# HL's stock market summary pages carry every constituent with a live delayed
# price, 110 rows to a page. Asking for a page past the last one returns the
# last one again, so the walk stops as soon as a page adds nothing new.
HL_PAGES = {
    "ftse-100": ("ftse-100", "FTSE 100", 100),
    "ftse-250": ("ftse-250", "FTSE 250", 250),
}


def hl_index(slug: str, label: str, expect: int) -> dict | None:
    seen: dict[str, dict] = {}
    for page in range(1, 6):
        url = f"https://www.hl.co.uk/shares/stock-market-summary/{slug}"
        if page > 1:
            url += f"?page={page}"
        h = get(url)
        if not h:
            break
        added = 0
        for row in rows_of(h):
            cells = cells_of(row)
            if len(cells) < 5 or cells[0].upper() == "EPIC":
                continue
            # EPIC | Name | price | change | pct | Deal
            change = next((c for c in cells[3:6] if "%" in c), None)
            v = pct(change) if change else None
            if v is None or cells[0] in seen:
                continue
            seen[cells[0]] = {"name": cells[1], "symbol": cells[0], "pct": v}
            added += 1
        if not added or len(seen) >= expect:
            break
    if len(seen) < expect * PRICED_FLOOR:   # a partial scrape is not a ranking
        print(f"  [fail] {label:12} HL returned {len(seen)} of {expect} constituents")
        return None
    # No index name in the chip: the heading beside it already reads "Top 3 -
    # Euro STOXX 50", and repeating it there ran "ALL 50 EURO STOXX 50
    # CONSTITUENTS" wide enough to break the header onto a second line.
    universe = (f"all {len(seen)} constituents" if len(seen) >= expect
                else f"{len(seen)} of {expect} constituents")
    return {"movers": list(seen.values()),
            "source": "HL market summary (delayed)", "universe": universe,
            "breadth": breadth_of({k: v["pct"] for k, v in seen.items()}),
            "how": f"{len(seen)} rows from HL's own summary pages"}


# ------------------------------------------------- Wikipedia + Yahoo pairs

def us_symbol(s: str) -> str | None:
    """Wikipedia writes class shares as BRK.B; Yahoo wants BRK-B."""
    s = (s or "").strip().replace(".", "-")
    return s if re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", s) else None


def hk_symbol(s: str) -> str | None:
    """'SEHK: 5' is Yahoo's 0005.HK - pad the board lot number to four digits."""
    m = re.search(r"(\d{1,5})", s or "")
    return f"{int(m.group(1)):04d}.HK" if m else None


def eu_symbol(s: str) -> str | None:
    """Already a Yahoo symbol, suffix and all - ADS.DE, AD.AS, NDA-FI.HE."""
    s = (s or "").strip()
    return s if re.fullmatch(r"[A-Z0-9]{1,6}(?:-[A-Z0-9]{1,4})?\.[A-Z]{2}", s) else None


def jp_symbol(s: str) -> str | None:
    """A Tokyo securities code - 7203 is Yahoo's 7203.T."""
    m = re.search(r"\b(\d{4})\b", s or "")
    return f"{m.group(1)}.T" if m else None


# Each index: where the list lives, how to read it, how many the index is
# meant to hold, and the fewest rows worth trusting. `nominal` is what the
# panel counts against, so a source that quietly drops names says "223 of 225"
# rather than a confident "all 223".
WIKI = {
    "s-p-500": {
        "label": "S&P 500", "nominal": 503, "floor": 480,
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "table": ("symbol", "security"), "map": us_symbol},
    # No nominal: the "100" holds ~102 securities, because a few members list
    # more than one share class. Counting against the list keeps the panel
    # honest about that rather than forcing it to a round number.
    "nasdaq-100": {
        "label": "Nasdaq 100", "nominal": None, "floor": 95,
        "url": "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
        "table": ("ticker", "company"), "map": us_symbol},
    "dow-jones": {
        "label": "Dow Jones", "nominal": 30, "floor": 28,
        "url": "https://en.wikipedia.org/wiki/"
               "List_of_Dow_Jones_Industrial_Average_companies",
        "table": ("symbol", "company"), "map": us_symbol},
    "hang-seng": {
        "label": "Hang Seng", "nominal": None, "floor": 75,
        "url": "https://en.wikipedia.org/wiki/Hang_Seng_Index",
        "table": ("ticker", "name"), "map": hk_symbol},
    "euro-stoxx-50": {
        "label": "Euro STOXX 50", "nominal": 50, "floor": 45,
        "url": "https://en.wikipedia.org/wiki/EURO_STOXX_50",
        "table": ("ticker", "name"), "map": eu_symbol},
    "nikkei-225": {
        "label": "Nikkei 225", "nominal": 225, "floor": 200,
        "url": "https://en.wikipedia.org/wiki/Nikkei_225",
        "list": ("Components", r"\(\s*TYO\s*:\s*\d{4}\s*\)"), "map": jp_symbol},
}


def wiki_table(h: str, sym_col: str, name_col: str,
               mapper, floor: int) -> list[tuple[str, str]] | None:
    """(symbol, company) from the table whose HEADER names both columns.

    Reading the header rather than sniffing cells for something ticker-shaped
    is what stops an Exchange column of "NYSE" being taken for 30 tickers, and
    a GICS Sector of "Industrials" being taken for 3M's company name.
    """
    for table in tables_of(h):
        rows = rows_of(table)
        if len(rows) < 2:
            continue
        header = [norm(c) for c in cells_of(rows[0])]
        if sym_col not in header or name_col not in header:
            continue
        si, ni = header.index(sym_col), header.index(name_col)
        got: list[tuple[str, str]] = []
        for row in rows[1:]:
            cells = cells_of(row)
            if len(cells) <= max(si, ni):
                continue
            sym, name = mapper(cells[si]), cells[ni].strip()
            if sym and name:
                got.append((sym, name))
        if len(got) >= floor:
            return got
    return None


def wiki_list(h: str, anchor: str, pattern: str,
              mapper, floor: int) -> list[tuple[str, str]] | None:
    """(symbol, company) from a bulleted list, for an index published as prose.

    The Nikkei's constituents are list items reading "Toyota Motor Corp
    (TYO: 7203)" grouped under sector headings, not a table. The name is
    whatever precedes the code, trimmed back past any citation bracket or
    section heading the list item swallowed - and it is only ever the cache's
    label anyway, since what the page shows comes from the price response.
    """
    start = h.find('id="' + anchor + '"')
    if start < 0:
        return None
    got: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in re.findall(r"<li[^>]*>(.*?)</li>", h[start:], re.S | re.I):
        text = text_of(item)
        m = re.search(pattern, text)
        if not m:
            continue
        sym = mapper(m.group(0))
        if not sym or sym in seen:
            continue
        name = re.split(r"[\]\n]", text[:m.start()])[-1].strip(" ., ")
        if name:
            seen.add(sym)
            got.append((sym, name))
    return got if len(got) >= floor else None


def wiki_tickers(cfg: dict) -> list[tuple[str, str]] | None:
    h = get(cfg["url"])
    if not h:
        return None
    if "table" in cfg:
        return wiki_table(h, *cfg["table"], cfg["map"], cfg["floor"])
    return wiki_list(h, *cfg["list"], cfg["map"], cfg["floor"])


def load_constituents(today: date) -> dict:
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    for key, cfg in WIKI.items():
        label = cfg["label"]
        entry = cache.get(key) or {}
        fetched = entry.get("fetched")
        fresh = False
        if fetched:
            try:
                fresh = (today - date.fromisoformat(fetched)).days < CONSTITUENT_MAX_AGE
            except ValueError:
                fresh = False
        if fresh and entry.get("tickers"):
            continue
        got = wiki_tickers(cfg)
        if got:
            cache[key] = {"fetched": today.isoformat(), "label": label,
                          "source": cfg["url"], "nominal": cfg["nominal"],
                          "tickers": [{"symbol": t, "name": n} for t, n in got]}
            print(f"  [list] {label:12} {len(got)} constituents refreshed")
        elif entry.get("tickers"):
            print(f"  [list] {label:12} refresh failed - keeping "
                  f"{len(entry['tickers'])} cached from {fetched}")
        else:
            print(f"  [list] {label:12} unavailable and nothing cached")
    CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    return cache


def day_pct(closes: list) -> float | None:
    """Last close against the one before it, as a percentage."""
    usable = [float(c) for c in closes
              if isinstance(c, (int, float)) and c is not None]
    if len(usable) < 2 or not usable[-2]:
        return None
    return (usable[-1] - usable[-2]) / usable[-2] * 100.0


def parse_spark(doc) -> dict[str, list]:
    """symbol -> close series, from either shape Yahoo has shipped for spark.

    The endpoint has served two formats over the years and neither is
    documented, so both are read and anything else is simply no data rather
    than an exception:

      flat        {"AAPL": {"symbol": "AAPL", "close": [...], ...}, ...}
      chart-like  {"spark": {"result": [{"symbol": "AAPL",
                              "response": [{"indicators": {"quote":
                              [{"close": [...]}]}}]}]}}
    """
    out: dict[str, list] = {}
    if not isinstance(doc, dict):
        return out

    results = ((doc.get("spark") or {}).get("result")
               if isinstance(doc.get("spark"), dict) else None)
    if isinstance(results, list):
        for row in results:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            for resp in (row.get("response") or []):
                if not isinstance(resp, dict):
                    continue
                try:
                    closes = resp["indicators"]["quote"][0]["close"]
                except (KeyError, IndexError, TypeError):
                    closes = resp.get("close")
                sym = sym or (resp.get("meta") or {}).get("symbol")
                if sym and isinstance(closes, list):
                    out[str(sym)] = closes
                    break
        return out

    for key, row in doc.items():
        if not isinstance(row, dict):
            continue
        closes = row.get("close")
        if isinstance(closes, list):
            out[str(row.get("symbol") or key)] = closes
    return out


def sweep(symbols: list[str], label: str) -> tuple[dict[str, float], str]:
    """Day moves for a whole constituent list. (symbol -> pct, how).

    Batched through spark, with a per-symbol fallback for whatever a batch
    could not price. If the first two batches yield nothing at all, spark is
    treated as unavailable for this run and the rest goes straight to the
    per-symbol path - one bad endpoint should cost twenty wasted requests,
    not twenty on top of a thousand.
    """
    out: dict[str, float] = {}
    batches = netfetch.chunked(symbols, SPARK_BATCH)
    spark_ok, sent = True, 0
    missed: list[str] = []

    for n, batch in enumerate(batches):
        wanted = set(batch)
        if spark_ok:
            url = SPARK_URL.format(
                urllib.parse.quote(",".join(batch), safe=","))
            sent += 1
            for sym, closes in parse_spark(
                    netfetch.fetch_json(url, ua=USER_AGENT)).items():
                # Only symbols we asked for. A batch endpoint answering with
                # something else is not a reason to price something else.
                if sym in wanted:
                    v = day_pct(closes)
                    if v is not None:
                        out[sym] = v
            if not out and n >= 1:
                # Two batches in and spark has produced nothing at all. It is
                # not having a bad moment; it has moved, or is refusing us.
                spark_ok = False
                print(f"  [warn] {label:12} spark returned nothing usable "
                      f"after {sent} batches - falling back to one request "
                      f"per symbol")
        missed += [s for s in batch if s not in out]

    if missed:
        for sym, v, _name in _confirm(missed):
            out[sym] = v

    if not spark_ok:
        how = f"{len(symbols)} per-symbol chart reads"
    elif missed:
        how = f"{sent} spark batches + {len(missed)} per-symbol"
    else:
        how = f"{sent} spark batches"
    return out, how


def read_move(doc) -> tuple[float, str] | None:
    """(day move, Yahoo's own name) from one chart response, or None.

    The name is the reason this path still exists at all now the sweep is
    batched: spark returns closes and nothing else, and a figure whose label
    came from somewhere other than the response that produced it is exactly
    the mislabelling the module docstring undertakes to prevent.
    """
    if not doc:
        return None
    try:
        r = doc["chart"]["result"][0]
        v = day_pct(r["indicators"]["quote"][0]["close"])
    except (KeyError, IndexError, TypeError):
        return None
    if v is None:
        return None
    meta = r.get("meta") or {}
    return v, (meta.get("longName") or meta.get("shortName") or "").strip()


def _confirm(symbols: list[str]) -> list[tuple[str, float, str]]:
    """Per-symbol chart reads, in input order, concurrent and rate-limited."""
    if not symbols:
        return []
    urls = [f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(s)}?interval=1d&range=5d" for s in symbols]
    out: list[tuple[str, float, str]] = []
    for sym, res in zip(symbols, netfetch.fetch_all(
            urls, workers=WORKERS, ua=USER_AGENT, tries=2)):
        hit = read_move(res.json())
        if hit:
            out.append((sym, hit[0], hit[1]))
    return out


def breadth_of(moves: dict[str, float]) -> dict:
    """How the whole index moved, not just its ends.

    The sweep prices every constituent and the panel shows six of them. That
    left the other ~987 readings on the floor, when between them they answer
    a question the top and bottom three cannot: was this a broad move or a
    handful of names? A day where 60 of 100 rose is a different market from
    one where 12 did, and both can show the same top three.

    Costs nothing - it is arithmetic on prices already fetched - and it is
    computed over exactly the constituents that priced, which the panel
    already states beside it.
    """
    vals = list(moves.values())
    if not vals:
        return {}
    up = sum(1 for v in vals if v > 0)
    down = sum(1 for v in vals if v < 0)
    return {"up": up, "down": down, "flat": len(vals) - up - down,
            "priced": len(vals), "median": round(median(vals), 2)}


def priced_index(key: str, cache: dict) -> dict | None:
    entry = cache.get(key)
    if not entry or not entry.get("tickers"):
        return None
    names = {t["symbol"]: t["name"] for t in entry["tickers"]}
    # Counted against the index's own size where it has a fixed one, so a
    # source that publishes 223 of the Nikkei's 225 cannot be reported as a
    # complete sweep of the index.
    total = entry.get("nominal") or len(names)
    moves, how = sweep(list(names), entry["label"])
    # A ranking is only honest if nearly the whole index priced.
    if len(moves) < total * PRICED_FLOOR:
        print(f"  [skip] {entry['label']:12} only {len(moves)} of "
              f"{total} priced - too incomplete to rank")
        return None

    ranked = sorted(moves.items(), key=lambda kv: -kv[1])
    breadth = breadth_of(moves)

    # Only the ends of the ranking are read a second time. The middle is
    # never published - rank() slices the top and bottom SHOWN - so naming
    # and re-checking all 500 would be buying nothing.
    #
    # CONFIRM_POOL, not SHOWN, because a refused row promotes the one behind
    # it. Confirming exactly three at each end and then dropping one would
    # push a fourth row into view carrying a Wikipedia name and a single
    # unchecked reading, which is the state this pass exists to prevent.
    pool = SHOWN * 3
    ends = [s for s, _ in ranked[:pool]] + [s for s, _ in ranked[-pool:]]
    ends = list(dict.fromkeys(ends))
    confirmed = {s: (v, nm) for s, v, nm in _confirm(ends)}

    def survivors(seq: list[tuple[str, float]]) -> tuple[list[dict], list[str]]:
        """Walk one end of the ranking, keeping rows both reads agree on."""
        kept: list[dict] = []
        refused: list[str] = []
        for sym, swept in seq:
            if len(kept) >= SHOWN:
                break
            hit = confirmed.get(sym)
            if hit is None:
                continue
            v, nm = hit
            if abs(v - swept) > CONFIRM_TOLERANCE:
                # The two reads describe different things. Which one is right
                # is not knowable from here, so neither is printed.
                refused.append(f"{sym} ({swept:+.2f}% sweep vs {v:+.2f}% read)")
                continue
            kept.append({"name": nm or names[sym], "symbol": sym, "pct": v})
        return kept, refused

    top, refused_top = survivors(ranked[:pool])
    bottom, refused_bot = survivors(ranked[-pool:][::-1])
    refused = refused_top + refused_bot
    if refused:
        print(f"  [drop] {entry['label']:12} {len(refused)} row(s) refused - "
              f"the two reads disagree: {', '.join(refused[:3])}")
    if len(top) < SHOWN or len(bottom) < SHOWN:
        # Neither end could be filled with rows that check out. A ranking
        # nobody can confirm is not a ranking.
        print(f"  [skip] {entry['label']:12} only {len(top)}/{len(bottom)} of "
              f"{SHOWN} top/bottom rows could be confirmed")
        return None

    universe = (f"all {total} constituents" if len(moves) >= total
                else f"{len(moves)} of {total} constituents")
    # Both ends, in one list: rank() sorts and slices it, and the middle of
    # the index was never going to be shown.
    return {"movers": top + bottom, "source": "Yahoo Finance daily closes",
            "universe": universe, "breadth": breadth, "how": how}


def breadth_line(got: dict) -> str:
    b = got.get("breadth") or {}
    if not b:
        return ""
    return (f"{b['up']} up / {b['down']} down / {b['flat']} flat, "
            f"median {b['median']:+.2f}%")


def rank(out: dict, key: str, label: str, got: dict, today: date) -> None:
    got["movers"].sort(key=lambda m: -m["pct"])
    entry = {
        "label": label, "asAt": today.isoformat(),
        "source": got["source"], "universe": got["universe"],
        "top": got["movers"][:SHOWN], "bottom": got["movers"][-SHOWN:][::-1]}
    # Breadth describes the whole index rather than its ends, and every price
    # it is computed from was already fetched to build the ranking above.
    if got.get("breadth"):
        entry["breadth"] = got["breadth"]
    out["indices"][key] = entry


# ---------------------------------------------------------------------------
# Self-test - the batch parsing and the breadth arithmetic, offline
# ---------------------------------------------------------------------------
def _selftest() -> bool:
    # Both shapes Yahoo has served for spark, and everything else read as
    # "no data" rather than as an exception. This is the parser the whole
    # constituent sweep now runs through, and the sandbox it was written in
    # cannot reach Yahoo - so it is pinned against captured shapes instead.
    flat = {"AAPL": {"symbol": "AAPL", "close": [100.0, 110.0],
                     "timestamp": [1, 2]},
            "MSFT": {"symbol": "MSFT", "close": [50.0, 49.0]}}
    got = parse_spark(flat)
    assert got == {"AAPL": [100.0, 110.0], "MSFT": [50.0, 49.0]}, got

    nested = {"spark": {"result": [
        {"symbol": "AAPL", "response": [
            {"meta": {"symbol": "AAPL"},
             "indicators": {"quote": [{"close": [100.0, 110.0]}]}}]},
        {"symbol": "MSFT", "response": [
            {"meta": {"symbol": "MSFT"},
             "indicators": {"quote": [{"close": [50.0, 49.0]}]}}]},
    ], "error": None}}
    assert parse_spark(nested) == {"AAPL": [100.0, 110.0],
                                   "MSFT": [50.0, 49.0]}, parse_spark(nested)

    assert parse_spark(None) == {}
    assert parse_spark({}) == {}
    assert parse_spark({"spark": {"result": []}}) == {}
    assert parse_spark({"finance": {"error": "x"}}) == {}
    assert parse_spark({"spark": {"result": [{"symbol": "A", "response": []}]}}) == {}
    print("  spark parse      OK  (both shapes read, junk -> no data)")

    # A day move is the last two closes. Nulls are padding, not prices.
    assert abs(day_pct([100.0, 110.0]) - 10.0) < 1e-9
    assert abs(day_pct([90.0, 100.0, None, 95.0]) + 5.0) < 1e-9
    assert day_pct([100.0]) is None, "one close is not a move"
    assert day_pct([]) is None
    assert day_pct([None, None]) is None
    assert day_pct([0.0, 5.0]) is None, "cannot divide by a zero close"
    print("  day move         OK  (nulls dropped, zero base refused)")

    # The per-symbol read that names a published row. Both the figure and
    # the label come out of the same response, which is the whole point.
    doc = {"chart": {"result": [{
        "meta": {"longName": "Antofagasta plc", "shortName": "ANTO.L"},
        "indicators": {"quote": [{"close": [100.0, 104.0]}]}}]}}
    got = read_move(doc)
    assert got and abs(got[0] - 4.0) < 1e-9 and got[1] == "Antofagasta plc", got
    assert read_move({"chart": {"result": []}}) is None
    assert read_move(None) is None
    assert read_move({"chart": {"result": [{"indicators": {"quote": [
        {"close": [100.0]}]}}]}}) is None, "one close is not a move"
    # A response with a price but no name still prices; the caller falls back
    # to the constituent list's label rather than dropping the row.
    got = read_move({"chart": {"result": [{
        "indicators": {"quote": [{"close": [10.0, 9.0]}]}}]}})
    assert got and got[1] == "", got
    print("  per-symbol read  OK  (figure and label from one response)")

    # Breadth is arithmetic on prices already fetched, over exactly the
    # constituents that priced.
    b = breadth_of({"a": 1.0, "b": 2.0, "c": -1.0, "d": 0.0})
    assert b == {"up": 2, "down": 1, "flat": 1, "priced": 4, "median": 0.5}, b
    assert breadth_of({}) == {}
    assert breadth_of({"a": -3.0})["median"] == -3.0
    print("  breadth          OK  (up/down/flat/median over what priced)")

    # The confirmation tolerance is what keeps a mis-mapped ticker off the
    # page: two reads of the same symbol minutes apart agree, two reads of
    # different things do not.
    assert CONFIRM_TOLERANCE > 0
    assert abs(0.31 - 0.28) <= CONFIRM_TOLERANCE, "normal drift must pass"
    assert abs(4.20 - (-1.10)) > CONFIRM_TOLERANCE, "a real disagreement must fail"

    # Batching is what turns 993 requests into 20.
    assert len(netfetch.chunked(list(range(993)), SPARK_BATCH)) == 20
    print("  batching         OK  (993 symbols -> 20 requests)")
    print("  index_movers self-test: OK")
    return True


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        print("Offline self-test:")
        _selftest()
        return 0
    today = date.today()
    out: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "indices": {}, "unsupported": dict(UNSUPPORTED)}

    print("Index constituent movers")
    built = 0

    for key, (slug, label, expect) in HL_PAGES.items():
        got = hl_index(slug, label, expect)
        if not got:
            # Say why rather than leaving the panel blank. A silent gap looks
            # like a bug; a stated reason is a fact about the data.
            out["unsupported"].setdefault(
                key, f"{label} could not be ranked on this run - HL's market "
                     f"summary did not return the full constituent list, and "
                     f"a partial index is not a ranking.")
            continue
        rank(out, key, label, got, today)
        built += 1
        print(f"  [ok]   {label:12} {got['universe']}  {breadth_line(got)}")

    cache = load_constituents(today)
    for key, cfg in WIKI.items():
        label = cfg["label"]
        got = priced_index(key, cache)
        if not got:
            out["unsupported"].setdefault(
                key, f"{label} could not be ranked on this run - the "
                     f"constituent list or its prices were unavailable, and a "
                     f"partial index is not a ranking.")
            continue
        rank(out, key, cache[key]["label"], got, today)
        built += 1
        print(f"  [ok]   {label:12} {got['universe']}  {breadth_line(got)}"
              f"  [{got.get('how', '')}]")

    if "--dry-run" in argv:
        print(f"\n[dry-run] {built} index/indices built, nothing written")
        print(json.dumps({k: v["universe"] for k, v in out["indices"].items()},
                         indent=2))
        return 0
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"\nwrote {OUT} ({built} indices ranked, "
          f"{len(out['unsupported'])} explained as unsupported)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
