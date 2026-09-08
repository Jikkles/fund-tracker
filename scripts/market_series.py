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
enough to ship as one JSON file. An index whose 1d range comes back empty
costs one more - see last_session(). The four go out together rather than
one after another: they are independent requests about the same index, and
netfetch's shared per-host rate limit is what keeps them polite, so the run
no longer sleeps a quarter of a second between each one.

The same responses also carry each index's 52-WEEK HIGH AND LOW, which were
being read past and dropped. They are published now - see range52() - because
a level means more against the year it sits in than on its own, and it costs
no request at all.

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
import urllib.error
import urllib.parse
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import netfetch

USER_AGENT = netfetch.UA_DESK
TIMEOUT = netfetch.DEFAULT_TIMEOUT
OUT = Path(__file__).resolve().parent.parent / "data" / "market.json"

# Tab order on the page. Equities first, then the commodity / rate / FX lines,
# which behave differently on a price chart and are better read last.
#
# The Nasdaq line here is the 100, not the Composite. The desk charts an index
# beside a ranking of its own constituents, and the Composite is the one index
# that cannot be ranked - ~3,000 names is not a load to put on a free price
# endpoint - so its tab only ever answered "this index is not broken down".
# The 100 is the same market read through the names that drive it, and all of
# it prices. See scripts/index_movers.py.
INDICES: list[tuple[str, str]] = [
    ("FTSE 100",         "^FTSE"),
    ("FTSE 250",         "^FTMC"),
    ("S&P 500",          "^GSPC"),
    ("Nasdaq 100",       "^NDX"),
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

# Tickers whose 1D line was rebuilt from the 5-day series this run, so the log
# says which tabs are showing a reconstructed session rather than a live one.
_REBUILT: set[str] = set()


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


def trading_periods(meta: dict) -> list[tuple[int, int]]:
    """Yahoo's own session boundaries for an intraday request.

    Arrives as a list of one-element lists, one per day. Anything that is not
    a pair of integers is dropped rather than repaired.
    """
    out: list[tuple[int, int]] = []
    for row in meta.get("tradingPeriods") or []:
        for p in (row if isinstance(row, list) else [row]):
            if not isinstance(p, dict):
                continue
            start, end = p.get("start"), p.get("end")
            if isinstance(start, int) and isinstance(end, int) and end > start:
                out.append((start, end))
    return sorted(set(out))


def last_session(parsed: dict) -> dict | None:
    """The most recent session in an intraday series, or None.

    Yahoo's 1d range means the *current* trading day. For a contract that
    trades nearly around the clock that day has not begun at 06:23 UTC on a
    Saturday, so GC=F and BZ=F come back with no bars at all, while ^FTSE
    still returns Friday because an index's current day resolves to the last
    day it actually traded. That asymmetry is upstream, not ours, and it left
    Gold and Brent as the only two tabs with no 1D line at the weekend.

    The session boundary is not inferred from gaps in the timestamps.
    meta.tradingPeriods is Yahoo's own statement of where a session starts and
    ends, so the slice is the source's definition of a session rather than
    this desk's guess at one. No tradingPeriods, no rebuild.
    """
    periods = trading_periods(parsed.get("meta") or {})
    if not periods:
        return None
    t, c = parsed["t"], parsed["c"]
    for start, end in reversed(periods):
        keep = [i for i, stamp in enumerate(t) if start <= stamp <= end]
        if len(keep) >= 2:
            return {"t": [t[i] for i in keep], "c": [c[i] for i in keep],
                    "start": start}
    return None


def prev_daily_close(daily: dict | None, start: int) -> float | None:
    """The daily close immediately before a session opens, or None.

    This is the baseline a rebuilt 1D line is measured from, and it is read
    off the daily series rather than taken from meta. chartPreviousClose on an
    empty 1d window is not the previous close: GC=F returned 4539.9 there on
    6 Sept 2026, a level that session neither opened nor closed at, while the
    daily bar for the day before said 4491.70. A baseline is a published
    figure or it is nothing - a wrong one silently rewrites the headline
    change printed beside it.
    """
    if not daily:
        return None
    earlier = [c for t, c in zip(daily["t"], daily["c"]) if t < start]
    return earlier[-1] if earlier else None


def series_url(ticker: str, rng: str, interval: str) -> str:
    return ("https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(ticker)}?interval={interval}&range={rng}")


def read_series(ticker: str, res: netfetch.Result) -> dict | None:
    """Turn one fetched chart response into a series, recording why not."""
    if not res:
        _LAST_ERROR[ticker] = res.error or "no response"
        return None
    parsed = parse_chart(res.body)
    if parsed is None:
        _LAST_ERROR[ticker] = (f"{len(res.body)} bytes that did not parse "
                               f"as a chart")
    return parsed


def fetch_series(ticker: str, rng: str, interval: str) -> dict | None:
    return read_series(ticker, netfetch.fetch(
        series_url(ticker, rng, interval), ua=USER_AGENT))


def clean_summary(raw: str) -> str:
    """Flatten a feed description into one plain-text paragraph, or "".

    Several publishers syndicated through this feed cap <description> at
    exactly 100 characters, so it arrives cut mid-word or mid-clause - "as
    investors analyzed comments from the U". There is no fuller version of
    that specific story to go and fetch: the article page carries no body in
    its HTML, and its own meta description is the same 100 characters. A
    trimmed, ellipsis-marked fragment was tried here and still read as half
    an idea rather than a summary, so a description with no sentence-ending
    punctuation is treated as unusable and dropped - the row then carries no
    summary at all, which the page already renders as a headline with no
    expandable note, rather than something invented to fill the gap.

    A description that already ends in real punctuation is complete, just
    sometimes long; that case is still capped for display, at a word
    boundary, with the cut marked - there the desk has the words, and Full
    story is one click away for the rest.
    """
    txt = re.sub(r"<[^>]+>", " ", raw or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    txt = txt.rstrip(" ,;:-–—")
    if not txt or txt[-1] not in ".!?…":
        return ""
    if len(txt) > NEWS_SUMMARY_MAX:
        cut = txt[:NEWS_SUMMARY_MAX]
        # Prefer breaking on a word boundary over slicing a word in half.
        space = cut.rfind(" ")
        if space > NEWS_SUMMARY_MAX * 0.6:
            cut = cut[:space]
        txt = cut.rstrip(" ,;:-–—") + "…"
    return txt


def fetch_news(ticker: str) -> list[dict]:
    """Headlines for one symbol. Always returns a list.

    News is decorative - a feed that is empty, slow or malformed must never
    cost us the chart, so every failure here degrades to no headlines rather
    than propagating. Not every symbol has a feed: ^FTMC returns nothing at
    all, and the panel says so rather than showing another index's news.
    """
    res = netfetch.fetch(NEWS_URL.format(urllib.parse.quote(ticker)),
                         ua=USER_AGENT, tries=2)
    if not res:
        return []
    try:
        root = ET.fromstring(res.body)
    except ET.ParseError:
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


def range52(meta: dict, daily: dict | None) -> dict | None:
    """The 52-week high and low, and which basis they are on.

    Both numbers arrive in the meta of every chart response this module
    already fetches, and were being read past and dropped. A price means
    more against the year it sits in than on its own - 26,140 is a different
    fact at the top of its range than at the bottom - and this costs nothing
    to publish.

    TWO BASES, NEVER MIXED, AND THE PAGE IS TOLD WHICH.
    Yahoo's fiftyTwoWeekHigh/Low are INTRADAY extremes: the highest price
    touched, not the highest close. The 1y series here is closes. Where Yahoo
    states the figures they are used as they come; where it does not, the
    range is computed from the closing series and labelled as closes, because
    quietly filling an intraday field with a closing number would make two
    indices incomparable while looking identical.

    Nothing is published at all when neither is available - the page then
    simply does not draw the band, which is the honest answer.
    """
    lo, hi = meta.get("fiftyTwoWeekLow"), meta.get("fiftyTwoWeekHigh")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > lo:
        return {"low": _round(float(lo)), "high": _round(float(hi)),
                "basis": "intraday"}
    if not daily or len(daily.get("c") or []) < 30:
        return None
    closes = [c for c in daily["c"] if isinstance(c, (int, float))]
    if len(closes) < 30 or max(closes) <= min(closes):
        return None
    return {"low": _round(min(closes)), "high": _round(max(closes)),
            "basis": "closes"}


def fetch_index(label: str, ticker: str) -> dict | None:
    """Build one index entry. None if not even the 1y series can be had."""
    series: dict[str, dict] = {}
    meta: dict = {}

    # The four ranges are independent requests about the same index, so they
    # go out together rather than one after another behind a fixed 0.25s
    # sleep. netfetch.fetch_all keeps them in SERIES order and the shared
    # rate limiter - not a sleep in this loop - is what keeps them polite.
    fetched = netfetch.fetch_all(
        [series_url(ticker, rng, interval) for _, rng, interval in SERIES],
        workers=len(SERIES), ua=USER_AGENT)

    for (key, _rng, _interval), res in zip(SERIES, fetched):
        got = read_series(ticker, res)
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

    # An empty 1d window is not the same as a broken feed - see last_session().
    # Rebuild the line from the 5-day series at 5m, sliced at Yahoo's own
    # session boundary. One extra request, only for the indices that came back
    # short, which in practice is the two commodity contracts at a weekend.
    if "1d" not in series:
        intraday = fetch_series(ticker, "5d", "5m")
        sess = last_session(intraday) if intraday else None
        if sess:
            entry = {"t": sess["t"], "c": sess["c"]}
            prev = prev_daily_close(series["1y"], sess["start"])
            if prev is not None:
                entry["prevClose"] = prev
            series["1d"] = entry
            _REBUILT.add(ticker)

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
        "range52": range52(meta, series.get("1y")),
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
        got = ",".join(k + ("*" if k == "1d" and ticker in _REBUILT else "")
                       for k in sorted(entry["series"]))
        pts = sum(len(s["c"]) for s in entry["series"].values())
        print(f"  [chart] {label:20} {ticker:11} "
              f"{entry['price']:>12}  {pts:5} pts  [{got}]  "
              f"{len(entry['news'])} headlines")
        out.append(entry)

    if _REBUILT:
        print("  [chart] * 1D rebuilt from the last completed session "
              "(Yahoo returned no bars for the current day)")

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

    # A description with no sentence-ending punctuation cannot be told apart
    # from a mid-word feed cut, so it is dropped rather than shown trimmed -
    # there is no fuller version of it to go and fetch. A complete sentence
    # is kept whole, tags stripped, however short.
    assert clean_summary("<p>Hello   <b>world</b></p>") == ""
    assert clean_summary("cut off mid clause,") == ""
    # The real shape of a 100-character feed cut, from three live headlines -
    # none of them recoverable, all of them dropped rather than trimmed.
    assert clean_summary(
        "The European stock markets closed higher in Friday trading as "
        "investors analyzed comments from the U") == ""
    assert clean_summary(
        "European stocks closed mostly lower in Tuesday trading amid surging "
        "inflation and escalat") == ""
    assert clean_summary(
        "Stocks fell in Wednesday trading as oil prices were flat amid "
        "possible") == ""
    # A complete sentence keeps every word, however short.
    assert clean_summary("Colman's Mustard has been put up for sale.") == (
        "Colman's Mustard has been put up for sale.")
    assert clean_summary("Ends properly.") == "Ends properly."
    # A description that is complete but long is still capped for display,
    # at a word boundary, marked with an ellipsis - the desk has these words,
    # it is only choosing not to show every one of them.
    long_txt = ("word " * 200).strip() + "."
    cut = clean_summary(long_txt)
    assert len(cut) <= NEWS_SUMMARY_MAX + 1 and cut.endswith("…"), cut
    assert clean_summary("") == ""
    print("  summary clean    OK  (unrecoverable cuts dropped, "
          "complete text capped and marked)")

    # The pair this exists to separate: a refusal that clears on its own, and
    # a fault that does not. Both used to print as a bare FAILED.
    throttled = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    assert netfetch.describe(throttled) == "HTTP 429 Too Many Requests"
    assert netfetch.describe(
        urllib.error.URLError("nodename nor servname provided")
    ).startswith("unreachable")
    assert netfetch.describe(TimeoutError()) == "timed out"
    print("  fetch reasons    OK  (status, unreachable and timeout kept apart)")

    # The weekend shape this exists for: an intraday series spanning several
    # sessions, of which only the last one is the 1D line. Boundaries are
    # Yahoo's, shaped the way it sends them - a list of one-element lists.
    day = 86400
    meta = {"tradingPeriods": [
        [{"start": 100, "end": 100 + day - 1}],
        [{"start": 100 + day, "end": 100 + 2 * day - 1}],
    ]}
    parsed = {"meta": meta,
              "t": [200, 300, 400, 100 + day + 50, 100 + day + 60, 100 + day + 70],
              "c": [10.0, 11.0, 12.0, 20.0, 21.0, 22.0]}
    sess = last_session(parsed)
    assert sess is not None and sess["c"] == [20.0, 21.0, 22.0], sess
    assert sess["start"] == 100 + day, sess["start"]
    print(f"  last session     OK  ({len(sess['c'])} of {len(parsed['c'])} bars kept, "
          "cut at Yahoo's own boundary)")

    # A session with a single print is not a line; fall back to the one before
    # it rather than drawing a dot and calling it a day.
    thin = {"meta": meta, "t": [200, 300, 100 + day + 50], "c": [10.0, 11.0, 20.0]}
    assert last_session(thin)["c"] == [10.0, 11.0], last_session(thin)
    # No boundaries published, no rebuild - the desk does not guess where a
    # 23-hour contract's day starts.
    assert last_session({"meta": {}, "t": [1, 2], "c": [1.0, 2.0]}) is None
    assert trading_periods({"tradingPeriods": [[{"start": 5, "end": 5}]]}) == []
    assert trading_periods({"tradingPeriods": [{"start": 1, "end": 2}]}) == [(1, 2)]
    print("  session guards   OK  (thin session skipped, no boundaries -> None)")

    # The baseline is the daily close *before* the session opens. The bar
    # stamped at the open belongs to the session being drawn, not before it.
    daily = {"t": [100 - day, 100, 100 + day], "c": [8.0, 9.0, 22.0]}
    assert prev_daily_close(daily, 100 + day) == 9.0
    assert prev_daily_close(daily, 100 - day) is None
    assert prev_daily_close(None, 100) is None
    print("  rebuilt baseline OK  (previous daily close, never the empty-window meta)")

    # The 52-week range comes out of meta that was already being fetched.
    # The two bases are never mixed and the basis travels with the numbers.
    got = range52({"fiftyTwoWeekLow": 7200.5, "fiftyTwoWeekHigh": 9100.25},
                  {"c": [1.0] * 300})
    assert got == {"low": 7200.5, "high": 9100.25, "basis": "intraday"}, got
    # No meta figures: computed from the closing series, and SAID to be.
    closes = {"c": [100.0 + (i % 37) for i in range(300)]}
    got = range52({}, closes)
    assert got == {"low": 100.0, "high": 136.0, "basis": "closes"}, got
    # Not enough history, a flat line, or nothing at all -> no band drawn.
    assert range52({}, {"c": [100.0] * 10}) is None
    assert range52({}, {"c": [100.0] * 300}) is None, "a flat year is not a range"
    assert range52({}, None) is None
    # A partial or inverted meta pair falls through to the closes rather than
    # being half-trusted.
    assert range52({"fiftyTwoWeekLow": 5.0}, closes)["basis"] == "closes"
    assert range52({"fiftyTwoWeekLow": 9.0, "fiftyTwoWeekHigh": 4.0},
                   closes)["basis"] == "closes"
    print("  52-week range    OK  (Yahoo's intraday figures preferred, "
          "closes labelled as such)")

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
