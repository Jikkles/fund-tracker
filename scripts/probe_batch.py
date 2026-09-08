"""
One-off: find out what Yahoo will actually answer for a BATCH of symbols.

The sandbox this desk is edited in cannot reach Yahoo, so the batching work
went in behind a per-symbol fallback and the first live run reported which
path it took. It took the fallback: spark returned nothing usable. This
prints what each candidate actually answers, from a runner that can reach it,
so the next change is made on evidence rather than on another guess.

Deleted once it has answered. Not wired into any scheduled workflow.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request

SYMS = ["AAPL", "MSFT", "NVDA"]
UA_DESK = "fund-tracker/1.0 (+github actions; personal research desk)"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
BROWSERISH = {"Accept": "application/json",
              "Origin": "https://finance.yahoo.com",
              "Referer": "https://finance.yahoo.com/"}

q = ",".join(SYMS)
CASES = [
    ("spark q1 minimal", UA_DESK, {},
     f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={q}"
     "&range=5d&interval=1d"),
    ("spark q1 full params", UA_DESK, {},
     f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={q}&range=5d"
     "&interval=1d&indicators=close&includeTimestamps=false"
     "&includePrePost=false&corsDomain=finance.yahoo.com&.tsrc=finance"),
    ("spark q2 minimal", UA_DESK, {},
     f"https://query2.finance.yahoo.com/v8/finance/spark?symbols={q}"
     "&range=5d&interval=1d"),
    ("spark q1 browser UA", UA_BROWSER, BROWSERISH,
     f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={q}"
     "&range=5d&interval=1d"),
    ("v7 quote", UA_BROWSER, BROWSERISH,
     f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={q}"),
    ("v6 quote", UA_BROWSER, BROWSERISH,
     f"https://query1.finance.yahoo.com/v6/finance/quote?symbols={q}"),
    ("chart (control, 1 symbol)", UA_DESK, {},
     "https://query1.finance.yahoo.com/v8/finance/chart/AAPL"
     "?interval=1d&range=5d"),
    ("stooq batch CSV", UA_DESK, {},
     "https://stooq.com/q/l/?s=aapl.us+msft.us+nvda.us&f=sd2t2ohlcv&h&e=csv"),
]


def main() -> int:
    for name, ua, extra, url in CASES:
        hdrs = {"User-Agent": ua, "Accept-Encoding": "identity"}
        hdrs.update(extra)
        req = urllib.request.Request(url, headers=hdrs)
        print(f"\n=== {name}\n    {url[:120]}")
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
                print(f"    HTTP {r.status}  {len(body)} bytes")
                print(f"    {body[:450]!r}")
        except urllib.error.HTTPError as e:
            print(f"    HTTP {e.code} {e.reason}")
            print(f"    {e.read()[:300]!r}")
        except Exception as e:
            print(f"    {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
