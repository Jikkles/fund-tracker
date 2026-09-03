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

Deliberately absent, with the reason recorded in the output so the page can
say it rather than showing an empty panel:

  Nasdaq Composite   ~3,000 constituents. Pricing them per symbol every run
                     is not a reasonable thing to do to a free endpoint.
  Nikkei 225         Wikipedia lists the 225 by company name only, with no
                     Tokyo ticker codes to price them by.
  Euro STOXX 50      the constituent list is available, but its 50 names are
                     spread across eight exchanges and mapping their tickers
                     onto price symbols is error-prone - a mis-mapped ticker
                     prices the wrong company.

  Gold, Brent,       not indices. They have no constituents, and saying so is
  10yr, GBP/USD      the correct output rather than an empty list.

WHY THE DISPLAYED NAME COMES FROM THE PRICE SOURCE
--------------------------------------------------
Wikipedia supplies the list of symbols; Yahoo supplies both the price and the
company name, read out of the same response. So the name beside a figure is
whatever the endpoint that produced the figure calls it, and the two cannot
disagree - the mis-mapping risk above stays a risk of pricing the wrong
company, never of labelling the right price with the wrong name.

The constituent lists change rarely, so they are cached in data/ and only
refetched when older than CONSTITUENT_MAX_AGE. Prices are always fresh.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "movers.json"
CACHE = ROOT / "data" / "constituents.json"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
TIMEOUT = 25
WORKERS = 8                 # polite concurrency against a free endpoint
CONSTITUENT_MAX_AGE = 7     # days before a cached constituent list is refetched
SHOWN = 3                   # rows per list; the panel sits beside the chart
PRICED_FLOOR = 0.9          # of the constituent list, below which it is a guess

# Indices the desk charts but deliberately does not rank, and why. The page
# prints these, so an absent panel explains itself instead of looking broken.
UNSUPPORTED = {
    "nasdaq-composite": "The Nasdaq Composite has around 3,000 constituents. "
                        "Pricing every one on each run is not a reasonable "
                        "load to put on a free data source.",
    "euro-stoxx-50": "The 50 constituents are spread across eight European "
                     "exchanges and mapping their tickers onto price symbols "
                     "is error-prone - a mis-mapped ticker would price the "
                     "wrong company.",
    "nikkei-225": "The Nikkei 225 is published as a list of company names "
                  "without Tokyo ticker codes, so there is nothing reliable "
                  "to price the constituents by.",
    "gold-usd-oz": "Not an index - a single commodity price, so it has no "
                   "constituents to rank.",
    "brent-crude": "Not an index - a single commodity price.",
    "us-10yr-yield": "Not an index - a single government bond yield.",
    "gbp-usd": "Not an index - a single exchange rate.",
}


def get(url: str, tries: int = 3) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
    return None


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
    universe = (f"all {len(seen)} {label} constituents" if len(seen) >= expect
                else f"{len(seen)} of {expect} {label} constituents")
    return {"movers": list(seen.values()),
            "source": "HL market summary (delayed)", "universe": universe}


# ------------------------------------------------- Wikipedia + Yahoo pairs

def us_symbol(s: str) -> str | None:
    """Wikipedia writes class shares as BRK.B; Yahoo wants BRK-B."""
    s = (s or "").strip().replace(".", "-")
    return s if re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", s) else None


def hk_symbol(s: str) -> str | None:
    """'SEHK: 5' is Yahoo's 0005.HK - pad the board lot number to four digits."""
    m = re.search(r"(\d{1,5})", s or "")
    return f"{int(m.group(1)):04d}.HK" if m else None


# key -> (article, symbol column, name column, symbol mapper, floor, label)
WIKI = {
    "s-p-500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                "symbol", "security", us_symbol, 480, "S&P 500"),
    "dow-jones": ("https://en.wikipedia.org/wiki/"
                  "List_of_Dow_Jones_Industrial_Average_companies",
                  "symbol", "company", us_symbol, 28, "Dow Jones"),
    "hang-seng": ("https://en.wikipedia.org/wiki/Hang_Seng_Index",
                  "ticker", "name", hk_symbol, 75, "Hang Seng"),
}


def wiki_tickers(url: str, sym_col: str, name_col: str,
                 mapper, floor: int) -> list[tuple[str, str]] | None:
    """(symbol, company) from the table whose HEADER names both columns.

    Reading the header rather than sniffing cells for something ticker-shaped
    is what stops an Exchange column of "NYSE" being taken for 30 tickers, and
    a GICS Sector of "Industrials" being taken for 3M's company name.
    """
    h = get(url)
    if not h:
        return None
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


def load_constituents(today: date) -> dict:
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    for key, (url, sym_col, name_col, mapper, floor, label) in WIKI.items():
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
        got = wiki_tickers(url, sym_col, name_col, mapper, floor)
        if got:
            cache[key] = {"fetched": today.isoformat(), "label": label,
                          "source": url,
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


def day_move(symbol: str) -> tuple[str, float, str] | None:
    """Last close against the one before it, with the name Yahoo files it under."""
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
         f"{urllib.parse.quote(symbol)}?interval=1d&range=5d")
    raw = get(u, tries=2)
    if not raw:
        return None
    try:
        r = json.loads(raw)["chart"]["result"][0]
        closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) < 2 or not closes[-2]:
            return None
        meta = r.get("meta") or {}
        name = (meta.get("longName") or meta.get("shortName") or "").strip()
        return symbol, (closes[-1] - closes[-2]) / closes[-2] * 100.0, name
    except Exception:
        return None


def priced_index(key: str, cache: dict) -> dict | None:
    entry = cache.get(key)
    if not entry or not entry.get("tickers"):
        return None
    names = {t["symbol"]: t["name"] for t in entry["tickers"]}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = [r for r in pool.map(day_move, list(names)) if r]
    # A ranking is only honest if nearly the whole index priced.
    if len(results) < len(names) * PRICED_FLOOR:
        print(f"  [skip] {entry['label']:12} only {len(results)} of "
              f"{len(names)} priced - too incomplete to rank")
        return None
    # Yahoo's own name for the symbol it has just priced, so a label cannot
    # belong to a different company than the figure beside it. The list's name
    # is the fallback for the rare symbol Yahoo prices without naming.
    movers = [{"name": nm or names[s], "symbol": s, "pct": v}
              for s, v, nm in results]
    return {"movers": movers, "source": "Yahoo Finance daily closes",
            "universe": f"{len(movers)} of {len(names)} "
                        f"{entry['label']} constituents"}


def rank(out: dict, key: str, label: str, got: dict, today: date) -> None:
    got["movers"].sort(key=lambda m: -m["pct"])
    out["indices"][key] = {
        "label": label, "asAt": today.isoformat(),
        "source": got["source"], "universe": got["universe"],
        "top": got["movers"][:SHOWN], "bottom": got["movers"][-SHOWN:][::-1]}


def main(argv: list[str]) -> int:
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
        print(f"  [ok]   {label:12} {got['universe']}")

    cache = load_constituents(today)
    for key, (_url, _sym, _name, _map, _floor, label) in WIKI.items():
        got = priced_index(key, cache)
        if not got:
            out["unsupported"].setdefault(
                key, f"{label} could not be ranked on this run - the "
                     f"constituent list or its prices were unavailable, and a "
                     f"partial index is not a ranking.")
            continue
        rank(out, key, cache[key]["label"], got, today)
        built += 1
        print(f"  [ok]   {label:12} {got['universe']}")

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
