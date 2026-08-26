"""
Price-history series for the index chart at the top of the page.

Yahoo's chart endpoint is the only free source here that publishes intraday
bars, and the 1D/5D ranges are the whole point of the panel, so this module
uses Yahoo alone rather than the Stooq-first ladder in market_data.py. Stooq
is daily-only and cannot serve those two ranges.

Four series are fetched per index and the rest are derived in the browser:

    1d   5m bars    the intraday line, redrawn each run
    5d   30m bars
    1y   1d bars    also sliced client-side into 1M / 6M / YTD
    5y   1wk bars

That is 4 requests per index rather than 7, and it keeps the payload small
enough to ship as one JSON file.

Failure is per-index, matching the rest of the desk: an index that will not
fetch is left out of the file and the panel simply does not offer that tab.
If *nothing* fetches, the run exits non-zero so the workflow fails and the
previous good deploy stays live rather than being replaced by an empty chart.

Output: data/market.json (a build artifact - not committed; regenerated on
every deploy).
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "fund-tracker/1.0 (+github actions; personal research desk)"
TIMEOUT = 25
OUT = Path(__file__).resolve().parent.parent / "data" / "market.json"

# Tab order on the page. Equities first, then the commodity / rate / FX lines,
# which behave differently on a price chart and are better read last.
INDICES: list[tuple[str, str]] = [
    ("FTSE 100",         "^FTSE"),
    ("FTSE 250",         "^FTMC"),
    ("S&P 500",          "^GSPC"),
    ("Nasdaq Composite", "^IXIC"),
    ("Dow Jones",        "^DJI"),
    ("Euro STOXX 50",    "^STOXX50E"),
    ("Nikkei 225",       "^N225"),
    ("Hang Seng",        "^HSI"),
    ("Gold (USD/oz)",    "GC=F"),
    ("Brent crude",      "BZ=F"),
    ("US 10yr yield",    "^TNX"),
    ("GBP/USD",          "GBPUSD=X"),
]

# (range key, Yahoo range, Yahoo interval)
SERIES: list[tuple[str, str, str]] = [
    ("1d", "1d", "5m"),
    ("5d", "5d", "30m"),
    ("1y", "1y", "1d"),
    ("5y", "5y", "1wk"),
]


def slug(label: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in label]
    out = "".join(keep)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _round(v: float) -> float:
    """Round to a precision that suits the magnitude.

    FX sits near 1.36 and needs four places; an index near 26,000 does not,
    and carrying the noise would inflate the payload for no visible gain.
    """
    a = abs(v)
    if a < 10:
        return round(v, 4)
    if a < 1000:
        return round(v, 3)
    return round(v, 2)


def parse_chart(raw: bytes) -> dict | None:
    """Pull timestamps, closes and quote metadata out of a chart payload."""
    try:
        result = json.loads(raw)["chart"]["result"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None

    stamps = result.get("timestamp") or []
    try:
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None

    # Yahoo pads the array with nulls for bars that never traded (holidays,
    # halts, the tail of a partial session). Drop those pairs entirely -
    # a gap is honest, an interpolated point is not.
    pairs = [(int(t), float(c)) for t, c in zip(stamps, closes)
             if t is not None and c is not None]
    if len(pairs) < 2:
        return None

    meta = result.get("meta") or {}
    return {
        "t": [p[0] for p in pairs],
        "c": [_round(p[1]) for p in pairs],
        "meta": meta,
    }


def fetch_series(ticker: str, rng: str, interval: str) -> dict | None:
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(ticker)}?interval={interval}&range={rng}")
    try:
        raw = _get(url)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return parse_chart(raw)


def fetch_index(label: str, ticker: str) -> dict | None:
    """Build one index entry. None if not even the 1y series can be had."""
    series: dict[str, dict] = {}
    meta: dict = {}

    for key, rng, interval in SERIES:
        got = fetch_series(ticker, rng, interval)
        time.sleep(0.25)                       # be polite to a free endpoint
        if got is None:
            continue
        meta = meta or got["meta"]
        entry = {"t": got["t"], "c": got["c"]}
        # The 1D line is drawn against the previous session's close, the way
        # a quote page does it, so the flat-open case still reads correctly.
        prev = got["meta"].get("chartPreviousClose")
        if key == "1d" and isinstance(prev, (int, float)):
            entry["prevClose"] = _round(float(prev))
        series[key] = entry

    # 1y is the spine: 1M, 6M and YTD are all sliced from it in the browser.
    # Without it the tab would offer ranges it cannot draw.
    if "1y" not in series:
        return None

    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose")
    if not isinstance(price, (int, float)):
        price = series["1y"]["c"][-1]

    return {
        "id": slug(label),
        "label": label,
        "ticker": ticker,
        "currency": meta.get("currency") or "",
        "tz": meta.get("exchangeTimezoneName") or "UTC",
        "price": _round(float(price)),
        "prevClose": _round(float(prev)) if isinstance(prev, (int, float)) else None,
        "asOf": int(meta.get("regularMarketTime") or 0) or None,
        "series": series,
    }


def build() -> dict:
    out, failed = [], []
    for label, ticker in INDICES:
        entry = fetch_index(label, ticker)
        if entry is None:
            failed.append(label)
            print(f"  [chart] {label:20} {ticker:11} FAILED")
            continue
        got = ",".join(sorted(entry["series"]))
        pts = sum(len(s["c"]) for s in entry["series"].values())
        print(f"  [chart] {label:20} {ticker:11} "
              f"{entry['price']:>12}  {pts:5} pts  [{got}]")
        out.append(entry)

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance chart API (unofficial, no key)",
        "failed": failed,
        "indices": out,
    }


# ---------------------------------------------------------------------------
# Self-test - parsing logic against captured payloads (runs offline)
# ---------------------------------------------------------------------------
def _selftest_offline() -> bool:
    payload = json.dumps({"chart": {"result": [{
        "timestamp": [1756200000, 1756200300, 1756200600, 1756200900],
        "indicators": {"quote": [{"close": [10880.0, None, 10885.5, 10886.25]}]},
        "meta": {"currency": "GBP", "exchangeTimezoneName": "Europe/London",
                 "chartPreviousClose": 10886.16, "regularMarketPrice": 10886.25,
                 "regularMarketTime": 1756200900},
    }]}}).encode()

    got = parse_chart(payload)
    assert got is not None, "chart parse failed"
    assert got["t"] == [1756200000, 1756200600, 1756200900],         f"null bar not dropped: {got['t']}"
    assert got["c"] == [10880.0, 10885.5, 10886.25], f"closes wrong: {got['c']}"
    print(f"  chart parse      OK  {len(got['c'])} pts "
          f"(1 null bar dropped, not interpolated)")

    assert parse_chart(b"not json") is None
    assert parse_chart(json.dumps({"chart": {"result": []}}).encode()) is None
    assert parse_chart(json.dumps({"chart": {"result": [{
        "timestamp": [1, 2],
        "indicators": {"quote": [{"close": [None, None]}]},
    }]}}).encode()) is None, "all-null series should be rejected"
    print("  failure handling OK  (malformed, empty, all-null all -> None)")

    # Precision has to follow magnitude: FX needs four places, an index does
    # not, and carrying the noise would inflate the payload for no gain.
    assert _round(1.36285) == 1.3629 or _round(1.36285) == 1.3628, _round(1.36285)
    assert _round(4.7041) == 4.7041
    assert _round(10886.2549) == 10886.25
    assert _round(87.2749) == 87.275
    print("  rounding         OK  (4dp under 10, 3dp under 1000, else 2dp)")

    assert slug("FTSE 100") == "ftse-100"
    assert slug("S&P 500") == "s-p-500"
    assert slug("Gold (USD/oz)") == "gold-usd-oz"
    print("  id slugs         OK")
    return True


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        print("Offline parsing self-test:")
        _selftest_offline()
        print("\nLive fetch check (needs network):")
        got = fetch_index("FTSE 100", "^FTSE")
        if got:
            print(f"  LIVE OK: {got['label']} {got['price']} {got['currency']}, "
                  f"series {sorted(got['series'])}")
        else:
            print("  LIVE FAILED: Yahoo unreachable. Check network/DNS before "
                  "relying on a scheduled run.")
        return 0

    dry = "--dry-run" in argv
    doc = build()

    if not doc["indices"]:
        print("\n[abort] no index fetched - leaving the previous file in place "
              "rather than publishing an empty chart.", file=sys.stderr)
        return 1

    payload = json.dumps(doc, separators=(",", ":"))
    kb = len(payload.encode()) / 1024
    print(f"\n{len(doc['indices'])} indices, {kb:.0f} KB"
          + (f", {len(doc['failed'])} failed: {', '.join(doc['failed'])}"
             if doc["failed"] else ""))

    if dry:
        print("[dry-run] nothing written")
        return 0

    OUT.write_text(payload, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
