"""
Free market data. No API key, no signup, no account.

Two independent sources, tried in order:

  1. Stooq  - plain CSV over HTTPS, no key, no rate limit worth worrying
              about, and stable for years. Primary.
  2. Yahoo  - the chart JSON endpoint, no key. Used when Stooq has no
              coverage for a symbol (it is patchy on some LSE ETFs and
              futures). Secondary.

Both are unofficial in the sense that neither publishes an SLA for this use.
That is the trade-off for £0 and zero credentials. The design assumption is
that a symbol WILL sometimes fail, so failure is handled per-symbol: a fund
whose proxy could not be priced is reported as unverified rather than
guessed, and the run still succeeds.

NOTE ON TESTING: the sandbox this was written in cannot reach either host,
so the live fetch paths could not be exercised here. The parsing logic is
unit-tested against captured payloads (see tests at the bottom). Run
`python scripts/market_data.py --selftest` on a machine with network access
before trusting a first live run.
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta

USER_AGENT = "fund-tracker/1.0 (+github actions; personal research desk)"
TIMEOUT = 20


@dataclass
class Quote:
    """A priced window for one symbol."""
    symbol: str
    start_date: date
    end_date: date
    start_price: float
    end_price: float
    source: str

    @property
    def pct_change(self) -> float:
        if not self.start_price:
            return 0.0
        return (self.end_price - self.start_price) / self.start_price * 100.0

    def format_pct(self) -> str:
        v = self.pct_change
        return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Stooq
# ---------------------------------------------------------------------------
def _stooq_symbol(ticker: str) -> str:
    """Translate a Yahoo-style symbol into Stooq's convention."""
    t = ticker.upper()
    if t.endswith(".L"):
        return t[:-2].lower() + ".uk"
    return {
        "^FTSE": "^ukx", "^FTMC": "^mcx", "^GSPC": "^spx", "^NDX": "^ndx",
        "^STOXX50E": "^stx50", "^N225": "^nkx", "^HSI": "^hsi",
        "GC=F": "gc.f", "BZ=F": "cb.f", "^TNX": "10usy.b",
        "GBPUSD=X": "gbpusd",
    }.get(t, t.lower())


def fetch_stooq(ticker: str, start: date, end: date) -> Quote | None:
    sym = _stooq_symbol(ticker)
    url = (f"https://stooq.com/q/d/l/?s={sym}"
           f"&d1={start:%Y%m%d}&d2={end:%Y%m%d}&i=d")
    try:
        raw = _get(url).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return _parse_stooq(raw, ticker)


def _parse_stooq(raw: str, ticker: str) -> Quote | None:
    if not raw or raw.lstrip().lower().startswith("no data"):
        return None
    rows = list(csv.DictReader(io.StringIO(raw)))
    rows = [r for r in rows if r.get("Close") not in (None, "", "-")]
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    try:
        return Quote(
            symbol=ticker,
            start_date=date.fromisoformat(first["Date"]),
            end_date=date.fromisoformat(last["Date"]),
            start_price=float(first["Close"]),
            end_price=float(last["Close"]),
            source="stooq",
        )
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Yahoo fallback
# ---------------------------------------------------------------------------
def fetch_yahoo(ticker: str, start: date, end: date) -> Quote | None:
    p1 = int(time.mktime(start.timetuple()))
    p2 = int(time.mktime((end + timedelta(days=1)).timetuple()))
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={p1}&period2={p2}&interval=1d")
    try:
        raw = _get(url)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return _parse_yahoo(raw, ticker)


def _parse_yahoo(raw: bytes, ticker: str) -> Quote | None:
    try:
        payload = json.loads(raw)
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None

    pairs = [(t, c) for t, c in zip(stamps, closes) if c is not None]
    if len(pairs) < 2:
        return None
    return Quote(
        symbol=ticker,
        start_date=date.fromtimestamp(pairs[0][0]),
        end_date=date.fromtimestamp(pairs[-1][0]),
        start_price=float(pairs[0][1]),
        end_price=float(pairs[-1][1]),
        source="yahoo",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch(ticker: str, start: date, end: date) -> Quote | None:
    """Price a window, trying each source. None if all fail."""
    for fetcher in (fetch_stooq, fetch_yahoo):
        quote = fetcher(ticker, start, end)
        if quote is not None:
            return quote
        time.sleep(0.3)          # be polite to free endpoints
    return None


def fetch_many(tickers: dict[str, str], start: date,
               end: date) -> dict[str, Quote]:
    """Price several labelled tickers. Missing entries simply absent."""
    out: dict[str, Quote] = {}
    for label, ticker in tickers.items():
        quote = fetch(ticker, start, end)
        if quote:
            out[label] = quote
            print(f"  [data] {label:20} {ticker:12} "
                  f"{quote.format_pct():>8}  ({quote.source})")
        else:
            print(f"  [data] {label:20} {ticker:12} "
                  f"{'FAILED':>8}  (all sources)")
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------------------
# Self-test - parsing logic against captured payloads (runs offline)
# ---------------------------------------------------------------------------
def _selftest_offline() -> bool:
    ok = True

    stooq_csv = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-07-28,10800.0,10880.0,10790.0,10846.00,0\n"
        "2026-08-10,10700.0,10760.0,10690.0,10720.00,0\n"
        "2026-08-25,10850.0,10890.0,10840.0,10868.00,0\n"
    )
    q = _parse_stooq(stooq_csv, "^FTSE")
    assert q and q.source == "stooq", "stooq parse failed"
    assert abs(q.pct_change - 0.2029) < 0.01, f"stooq pct wrong: {q.pct_change}"
    print(f"  stooq parse      OK  {q.format_pct()} "
          f"({q.start_date} -> {q.end_date})")

    yahoo_json = json.dumps({"chart": {"result": [{
        "timestamp": [1754006400, 1755216000, 1756080000],
        "indicators": {"quote": [{"close": [100.0, None, 104.5]}]},
    }]}}).encode()
    q2 = _parse_yahoo(yahoo_json, "TEST.L")
    assert q2 and abs(q2.pct_change - 4.5) < 0.001, "yahoo pct wrong"
    print(f"  yahoo parse      OK  {q2.format_pct()} (None values skipped)")

    assert _parse_stooq("No data\n", "X") is None
    assert _parse_stooq("Date,Close\n2026-01-01,5\n", "X") is None, \
        "single row should be rejected"
    assert _parse_yahoo(b"not json", "X") is None
    assert _parse_yahoo(json.dumps({"chart": {"result": []}}).encode(),
                        "X") is None
    print("  failure handling OK  (empty, single-row, malformed all -> None)")

    assert _stooq_symbol("ISF.L") == "isf.uk"
    assert _stooq_symbol("^FTSE") == "^ukx"
    assert _stooq_symbol("GC=F") == "gc.f"
    print("  symbol mapping   OK")
    return ok


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        print("Offline parsing self-test:")
        _selftest_offline()
        print("\nLive fetch check (needs network):")
        today = date.today()
        q = fetch("^FTSE", today - timedelta(days=30), today)
        if q:
            print(f"  LIVE OK: FTSE 100 {q.format_pct()} via {q.source} "
                  f"({q.start_date} -> {q.end_date})")
        else:
            print("  LIVE FAILED: neither source reachable. "
                  "Check network/DNS before relying on a scheduled run.")
    else:
        today = date.today()
        from proxies import CONTEXT_TICKERS
        fetch_many(CONTEXT_TICKERS, today - timedelta(days=30), today)
