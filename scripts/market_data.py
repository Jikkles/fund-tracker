"""
Free market data. No API key, no signup, no account.

One source: Yahoo's chart JSON endpoint, no key.

There used to be a second, and it was tried first. Stooq began answering with
a JavaScript bot-check page instead of CSV, so it stopped supplying prices -
all 255 quotes in the ten daily runs to 2026-11-01 came from Yahoo - while
still being asked for every ticker. Usually that wasted one request. When
stooq.com was also slow to answer it cost about 153s a ticker before Yahoo was
even tried, which ran "Run the update" into its ten-minute limit on 2026-10-18,
10-23 and 10-27 and threw away those runs' NAV refresh. A fallback that cannot
return data only adds a way to fail.

Yahoo is unofficial in the sense that it publishes no SLA for this use. That
is the trade-off for £0 and zero credentials. The design assumption is that a
symbol WILL sometimes fail, so failure is handled per-symbol: a fund whose
proxy could not be priced is reported as unverified rather than guessed, and
the run still succeeds.

`python scripts/market_data.py --selftest` checks the parsing offline and then
makes one live fetch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

import netfetch

USER_AGENT = netfetch.UA_DESK
TIMEOUT = netfetch.DEFAULT_TIMEOUT


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


def _get(url: str) -> bytes | None:
    """Bytes, or None. Retries and per-host pacing come from netfetch."""
    return netfetch.fetch(url, ua=USER_AGENT).body


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def fetch(ticker: str, start: date, end: date) -> Quote | None:
    """Price a window. None if it could not be priced."""
    # A calendar date, not time.mktime on a date tuple: mktime reads the
    # struct in LOCAL time, and a runner in a different zone than the author
    # shifts the window by an hour either way. UTC midnight is the same
    # instant everywhere.
    p1 = int(datetime.combine(start, dtime.min, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.combine(end + timedelta(days=1), dtime.min,
                              tzinfo=timezone.utc).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={p1}&period2={p2}&interval=1d")
    raw = _get(url)
    if raw is None:
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
                  f"{'FAILED':>8}  (yahoo)")
    return out


# ---------------------------------------------------------------------------
# Self-test - parsing logic against captured payloads (runs offline)
# ---------------------------------------------------------------------------
def _selftest_offline() -> bool:
    ok = True

    yahoo_json = json.dumps({"chart": {"result": [{
        "timestamp": [1754006400, 1755216000, 1756080000],
        "indicators": {"quote": [{"close": [100.0, None, 104.5]}]},
    }]}}).encode()
    q2 = _parse_yahoo(yahoo_json, "TEST.L")
    assert q2 and abs(q2.pct_change - 4.5) < 0.001, "yahoo pct wrong"
    print(f"  yahoo parse      OK  {q2.format_pct()} (None values skipped)")

    one_row = json.dumps({"chart": {"result": [{
        "timestamp": [1754006400],
        "indicators": {"quote": [{"close": [100.0]}]},
    }]}}).encode()
    assert _parse_yahoo(one_row, "X") is None, "single row should be rejected"
    assert _parse_yahoo(b"not json", "X") is None
    assert _parse_yahoo(json.dumps({"chart": {"result": []}}).encode(),
                        "X") is None
    print("  failure handling OK  (empty, single-row, malformed all -> None)")

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
            print("  LIVE FAILED: Yahoo not reachable. "
                  "Check network/DNS before relying on a scheduled run.")
    else:
        today = date.today()
        from proxies import CONTEXT_TICKERS
        fetch_many(CONTEXT_TICKERS, today - timedelta(days=30), today)
