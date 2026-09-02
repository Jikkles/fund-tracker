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

Output: data/market.json. The daily run commits it; the chart-refresh
workflow regenerates it and ships it straight to the Pages artifact without
committing, so the served copy is usually fresher than the one on main.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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

# Yahoo's headline feed is per-symbol, so each index gets news about itself
# rather than a general business wire. That is what makes the panel relevant
# by construction - there is no judgement call about what "affects markets".
NEWS_URL = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
            "?s={}&region=US&lang=en-US")
NEWS_PER_INDEX = 6
NEWS_SUMMARY_MAX = 400

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


# Why the most recent fetch for a ticker failed, keyed by ticker and read by
# build() when an index comes back with nothing. A module-level note rather
# than a changed return type: every caller here wants the data or None, and
# only the run log wants the reason.
_LAST_ERROR: dict[str, str] = {}


def _describe(exc: BaseException) -> str:
    """Render a failed fetch as one readable line.

    HTTPError subclasses URLError, so catching URLError alone collapses a 429,
    a 404 and a DNS failure into the same silent None - which is what made a
    whole-panel failure unreadable from the run log. The distinction is the
    diagnosis: a status code means Yahoo answered and refused us, which is
    waited out, while an unreachable host or an unparseable body means
    something moved and the code has to follow.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, TimeoutError):
            return f"timeout after {TIMEOUT}s"
        return f"unreachable ({exc.reason})"
    if isinstance(exc, TimeoutError):
        return f"timeout after {TIMEOUT}s"
    return f"{type(exc).__name__}: {exc}"


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
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _LAST_ERROR[ticker] = _describe(exc)
        return None
    parsed = parse_chart(raw)
    if parsed is None:
        _LAST_ERROR[ticker] = f"{len(raw)} bytes that did not parse as a chart"
    return parsed


def clean_summary(raw: str) -> str:
    """Flatten a feed description into one plain-text paragraph.

    Descriptions arrive with markup in them and are already truncated by the
    feed, usually mid-sentence. Trailing punctuation is normalised to an
    ellipsis so the cut is visible rather than reading as a typo.
    """
    txt = re.sub(r"<[^>]+>", " ", raw or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > NEWS_SUMMARY_MAX:
        cut = txt[:NEWS_SUMMARY_MAX]
        # Prefer breaking on a word boundary over slicing a word in half.
        space = cut.rfind(" ")
        if space > NEWS_SUMMARY_MAX * 0.6:
            cut = cut[:space]
        txt = cut
    txt = txt.rstrip(" ,;:-–—")
    if txt and txt[-1] not in ".!?…":
        txt += "…"
    return txt


def fetch_news(ticker: str) -> list[dict]:
    """Headlines for one symbol. Always returns a list.

    News is decorative - a feed that is empty, slow or malformed must never
    cost us the chart, so every failure here degrades to no headlines rather
    than propagating. Not every symbol has a feed: ^FTMC returns nothing at
    all, and the panel says so rather than showing another index's news.
    """
    try:
        raw = _get(NEWS_URL.format(urllib.parse.quote(ticker)))
        root = ET.fromstring(raw)
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError):
        return []

    out, seen = [], set()
    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())

        when = None
        stamp_txt = item.findtext("pubDate")
        if stamp_txt:
            try:
                when = int(parsedate_to_datetime(stamp_txt).timestamp())
            except (TypeError, ValueError):
                when = None

        out.append({"t": title, "u": link, "d": when,
                    "s": clean_summary(item.findtext("description") or "")})
        if len(out) >= NEWS_PER_INDEX:
            break
    return out


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
        "news": fetch_news(ticker),
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
            why = _LAST_ERROR.get(ticker, "no 1y series returned")
            print(f"  [chart] {label:20} {ticker:11} FAILED  {why}")
            continue
        got = ",".join(sorted(entry["series"]))
        pts = sum(len(s["c"]) for s in entry["series"].values())
        print(f"  [chart] {label:20} {ticker:11} "
              f"{entry['price']:>12}  {pts:5} pts  [{got}]  "
              f"{len(entry['news'])} headlines")
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

    # An unpunctuated description is almost always a feed truncation, so it
    # earns the ellipsis; one that already ends in a full stop does not.
    assert clean_summary("<p>Hello   <b>world</b></p>") == "Hello world…"
    assert clean_summary("cut off mid clause,").endswith("clause…"),         clean_summary("cut off mid clause,")
    assert clean_summary("Ends properly.") == "Ends properly."
    long_txt = "word " * 200
    assert len(clean_summary(long_txt)) <= NEWS_SUMMARY_MAX + 1
    assert clean_summary("") == ""
    print("  summary clean    OK  (tags stripped, truncation marked)")

    # The pair this exists to separate: a refusal that clears on its own, and
    # a fault that does not. Both used to print as a bare FAILED.
    throttled = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    assert _describe(throttled) == "HTTP 429 Too Many Requests", _describe(throttled)
    assert _describe(urllib.error.URLError("nodename nor servname provided")) \
        .startswith("unreachable"), _describe(urllib.error.URLError("x"))
    assert _describe(TimeoutError()) == f"timeout after {TIMEOUT}s"
    print("  fetch reasons    OK  (status, unreachable and timeout kept apart)")

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
            why = _LAST_ERROR.get("^FTSE", "no 1y series returned")
            print(f"  LIVE FAILED: {why}. Check network/DNS before relying "
                  "on a scheduled run.")
        return 0

    dry = "--dry-run" in argv
    doc = build()

    if not doc["indices"]:
        print("\n[abort] no index fetched - leaving the previous file in place "
              "rather than publishing an empty chart.", file=sys.stderr)
        # Every index failing at once is nearly always one upstream cause, so
        # the distinct reasons are a short list and belong in the abort itself.
        # Without them the log says only FAILED twelve times, and a refusal
        # that clears on its own reads exactly like an endpoint that moved.
        seen = sorted(set(_LAST_ERROR.values()))
        print(f"[abort] reason{'s' if len(seen) > 1 else ''}: "
              + ("; ".join(seen) if seen else "unknown"), file=sys.stderr)
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
