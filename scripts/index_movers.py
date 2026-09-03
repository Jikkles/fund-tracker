"""
Top and bottom movers among the CONSTITUENTS of each market index.

WHY ONLY SOME INDICES
---------------------
"Top 5 in the FTSE 100" is only true if all 100 were priced. Ranking 30 of
them and calling the best five a top five is exactly the kind of confident,
unfounded claim this desk exists not to make. So an index appears here only
where its full constituent list can be obtained and priced:

  FTSE 100     HL publish the whole index with prices on one page - one
               request, and it is the same platform the desk buys through.
  S&P 500      constituent list from Wikipedia, prices from Yahoo.
  Dow Jones    same, and only 30 names.

Deliberately absent, with the reason recorded in the output so the page can
say it rather than showing an empty panel:

  Nasdaq Composite   ~3,000 constituents. Pricing them per symbol every run
                     is not a reasonable thing to do to a free endpoint.
  FTSE 250/350       HL's summary pages return only ~114 rows, so the list is
                     truncated and any ranking off it would be a guess.
  Euro STOXX 50,     constituent lists are available, but mapping their
  Nikkei, Hang Seng  tickers onto Yahoo symbols is error-prone across several
                     exchanges, and a mis-mapped ticker prints a real price
                     against the wrong company. Not worth the risk for five
                     rows of a panel.

  Gold, Brent,       not indices. They have no constituents, and saying so is
  10yr, GBP/USD      the correct output rather than an empty list.

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
SHOWN = 5

# Indices the desk charts but deliberately does not rank, and why. The page
# prints these, so an absent panel explains itself instead of looking broken.
UNSUPPORTED = {
    "ftse-250": "HL publish only part of the FTSE 250 on one page, so the "
                "constituent list would be incomplete and any ranking a guess.",
    "nasdaq-composite": "The Nasdaq Composite has around 3,000 constituents. "
                        "Pricing every one on each run is not a reasonable "
                        "load to put on a free data source.",
    "euro-stoxx-50": "Constituents span several European exchanges and mapping "
                     "their tickers onto price symbols is error-prone - a "
                     "mis-mapped ticker shows a real price against the wrong "
                     "company.",
    "nikkei-225": "Same reason as the Euro STOXX 50: cross-exchange ticker "
                  "mapping is not reliable enough to publish from.",
    "hang-seng": "Same reason as the Euro STOXX 50.",
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


def pct(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return float(m.group(0)) if m else None


# --------------------------------------------------------------- FTSE 100

def ftse100() -> dict | None:
    """HL publish the whole index, with prices, on one page."""
    h = get("https://www.hl.co.uk/shares/stock-market-summary/ftse-100")
    if not h:
        return None
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S | re.I):
        cells = [text_of(c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        if len(cells) < 5 or cells[0].upper() == "EPIC":
            continue
        # EPIC | Name | price | change | pct | Deal
        change = next((c for c in cells[3:6] if "%" in c), None)
        v = pct(change) if change else None
        if v is None:
            continue
        out.append({"name": cells[1], "symbol": cells[0], "pct": v})
    if len(out) < 90:                      # a partial scrape is not a ranking
        return None
    return {"movers": out, "source": "HL market summary (delayed)",
            "universe": f"all {len(out)} FTSE 100 constituents"}


# ------------------------------------------------- Wikipedia + Yahoo pairs

WIKI = {
    "s-p-500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                "S&P 500", 480),
    "dow-jones": ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
                  "Dow Jones", 28),
}


def wiki_tickers(url: str, floor: int) -> list[tuple[str, str]] | None:
    """(ticker, company) pairs from the first table that looks like a list."""
    h = get(url)
    if not h:
        return None
    best: list[tuple[str, str]] = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", h, re.S | re.I):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)
        got: list[tuple[str, str]] = []
        for row in rows:
            cells = [text_of(c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            if len(cells) < 2:
                continue
            for i, c in enumerate(cells[:3]):
                # A US ticker: 1-5 upper-case letters, optionally dotted.
                if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", c):
                    name = next((x for j, x in enumerate(cells)
                                 if j != i and len(x) > 3), c)
                    got.append((c, name))
                    break
        if len(got) > len(best):
            best = got
    return best if len(best) >= floor else None


def load_constituents(today: date) -> dict:
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    for key, (url, label, floor) in WIKI.items():
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
        got = wiki_tickers(url, floor)
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


def day_move(symbol: str) -> tuple[str, float] | None:
    """Last close against the one before it."""
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
        return symbol, (closes[-1] - closes[-2]) / closes[-2] * 100.0
    except Exception:
        return None


def priced_index(key: str, cache: dict) -> dict | None:
    entry = cache.get(key)
    if not entry or not entry.get("tickers"):
        return None
    tickers = entry["tickers"]
    names = {t["symbol"]: t["name"] for t in tickers}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = [r for r in pool.map(day_move, list(names)) if r]
    # A ranking is only honest if nearly the whole index priced.
    if len(results) < len(names) * 0.9:
        print(f"  [skip] {entry['label']:12} only {len(results)} of "
              f"{len(names)} priced - too incomplete to rank")
        return None
    movers = [{"name": names[s], "symbol": s, "pct": v} for s, v in results]
    return {"movers": movers, "source": "Yahoo Finance daily closes",
            "universe": f"{len(movers)} of {len(names)} "
                        f"{entry['label']} constituents"}


def main(argv: list[str]) -> int:
    today = date.today()
    out: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "indices": {}, "unsupported": UNSUPPORTED}

    print("Index constituent movers")
    built = 0
    if (f := ftse100()):
        f["movers"].sort(key=lambda m: -m["pct"])
        out["indices"]["ftse-100"] = {
            "label": "FTSE 100", "asAt": today.isoformat(),
            "source": f["source"], "universe": f["universe"],
            "top": f["movers"][:SHOWN], "bottom": f["movers"][-SHOWN:][::-1]}
        built += 1
        print(f"  [ok]   FTSE 100     {f['universe']}")
    else:
        print("  [fail] FTSE 100     HL page did not yield a full constituent list")

    cache = load_constituents(today)
    for key in WIKI:
        got = priced_index(key, cache)
        if not got:
            # Say why rather than leaving the panel blank. A silent gap looks
            # like a bug; a stated reason is a fact about the data.
            out["unsupported"].setdefault(
                key, f"{WIKI[key][1]} could not be ranked on this run - the "
                     f"constituent list or its prices were unavailable, and a "
                     f"partial index is not a ranking.")
            continue
        got["movers"].sort(key=lambda m: -m["pct"])
        out["indices"][key] = {
            "label": cache[key]["label"], "asAt": today.isoformat(),
            "source": got["source"], "universe": got["universe"],
            "top": got["movers"][:SHOWN], "bottom": got["movers"][-SHOWN:][::-1]}
        built += 1
        print(f"  [ok]   {cache[key]['label']:12} {got['universe']}")

    if "--dry-run" in argv:
        print(f"\n[dry-run] {built} index/indices built, nothing written")
        return 0
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"\nwrote {OUT} ({built} indices ranked, "
          f"{len(UNSUPPORTED)} explained as unsupported)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
