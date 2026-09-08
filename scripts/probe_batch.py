"""
One-off: why did the batched sweep get nothing out of Yahoo's spark endpoint?

Round one established that spark itself is fine - four URL variants, all HTTP
200, all the flat {"SYM": {"close": [...]}} shape the parser handles. So the
fault is in this repo, and round two runs THE REAL CODE PATH against THE REAL
symbols instead of three hand-picked American tickers.

Deleted once it has answered.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import index_movers as im
import netfetch

CACHE = Path(__file__).resolve().parent.parent / "data" / "constituents.json"


def report(label: str, symbols: list[str]) -> None:
    url = im.SPARK_URL.format(urllib.parse.quote(",".join(symbols), safe=","))
    res = netfetch.fetch(url, ua=im.USER_AGENT)
    print(f"\n=== {label}: {len(symbols)} symbols, URL {len(url)} chars")
    print(f"    {url[:160]}")
    print(f"    HTTP {res.status}  error={res.error!r}  "
          f"bytes={len(res.body) if res.body else 0}")
    doc = res.json()
    if doc is None:
        print(f"    body did not parse. first 300: {(res.body or b'')[:300]!r}")
        return
    if isinstance(doc, dict):
        print(f"    top-level keys: {list(doc)[:8]}{' ...' if len(doc) > 8 else ''}")
    parsed = im.parse_spark(doc)
    print(f"    parse_spark -> {len(parsed)} symbols")
    wanted = set(symbols)
    hit = [k for k in parsed if k in wanted]
    print(f"    of which we asked for: {len(hit)}")
    if parsed and not hit:
        print(f"    RETURNED KEYS NOT IN OUR LIST: {list(parsed)[:6]}")
    if hit:
        k = hit[0]
        print(f"    sample {k}: closes={parsed[k][:5]} -> "
              f"day_pct={im.day_pct(parsed[k])}")


def main() -> int:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    dow = [t["symbol"] for t in cache["dow-jones"]["tickers"]]
    spx = [t["symbol"] for t in cache["s-p-500"]["tickers"]]
    nikkei = [t["symbol"] for t in cache["nikkei-225"]["tickers"]]

    # Round two found the cap sits between 10 (HTTP 200) and 25 (HTTP 400,
    # empty body). Bisect it, on two different boards so the answer is about
    # the count rather than about one exchange's symbols.
    for n in (11, 12, 14, 15, 16, 18, 20, 24):
        report(f"S&P first {n}", spx[:n])
    for n in (10, 15, 20):
        report(f"Nikkei first {n} (.T suffix)", nikkei[:n])
    report(f"Dow all {len(dow)}", dow)
    return 0


if __name__ == "__main__":
    sys.exit(main())
