"""
Refresh the Premium Bonds prize data the calculator tab runs on.

WHY THIS EXISTS
---------------
The calculator's figures - the prize fund rate, the odds and the count of
every prize value - change whenever NS&I changes them and drift slightly every
month as the number of eligible Bonds moves. They were typed into the page by
hand, so they were only as current as the last time someone remembered. This
reads them from NS&I's own pages once a week (weekly-factsheets.yml) and
writes data/premium_bonds.json, which the page fetches.

WHERE THE FIGURES COME FROM
---------------------------
Two NS&I pages, because neither carries everything:

  * the monthly prize allocation page - a table of every prize value with the
    count for the current draw and an estimate for the next, plus the total
    number of prizes and their total value.
  * the Premium Bonds product page - the annual prize fund rate and the odds
    ("21,000 to 1 for every £1 Bond").

WHAT IT REFUSES TO DO
---------------------
Nothing is written unless every gate below passes. A refusal prints SKIP with
the reason and leaves the stored file exactly as it was; the page then shows
the date of the last good check, so a run of refusals is visible rather than
silent.

  1. THE TABLE ADDS UP. The prize counts must sum to NS&I's own "Total
     prizes" row and count x value must sum to its "Total value" row. A table
     that parsed into the wrong cells cannot do both.
  2. ONE RATE, ONE ODDS. The product page also carries change notices
     ("3.30%, variable (until the June 2026 prize draw)"). Every figure
     qualified by "until" is history and dropped; the rest must agree.
  3. THE PAGES AGREE WITH EACH OTHER. The prize fund is set so that
     12 x total value / (prizes x odds) is the annual rate. The two pages are
     published separately, so if that does not reproduce the stated rate to
     within 0.02pp, one of them is mid-change and neither is trusted.
  4. NO GOING BACKWARDS. A draw month older than the one already stored is
     refused, so a cached or rolled-back page cannot replace newer figures.

Standard library only, like everything else here.

    python scripts/premium_bonds.py              # refresh data/premium_bonds.json
    python scripts/premium_bonds.py --dry-run    # parse and check, write nothing
    python scripts/premium_bonds.py --selftest   # gates against fixtures, offline
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import sys
from datetime import date, datetime
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netfetch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "premium_bonds.json")
ALLOCATION_URL = "https://www.nsandi.com/get-to-know-us/monthly-prize-allocation"
PRODUCT_URL = "https://www.nsandi.com/products/premium-bonds"
RATE_TOLERANCE_PP = 0.02
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


class Refused(Exception):
    """A gate failed. The message is the SKIP line."""


# ---------- the allocation table ----------

class _Tables(HTMLParser):
    """Every <table> on the page as a list of rows of cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _money(text: str) -> int | None:
    """'£1 million' -> 1000000, '£100,000' -> 100000, '£498,378,775' -> 498378775."""
    m = re.search(r"£\s*([\d,]+(?:\.\d+)?)\s*(million|m\b)?", text.replace("\xa0", " "), re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return int(round(v * 1_000_000)) if m.group(2) else int(round(v))


def _count(text: str) -> int | None:
    t = text.replace(",", "").replace("\xa0", "").strip()
    return int(t) if t.isdigit() else None


def parse_allocation(page: str) -> dict:
    """The current draw's prize table, checked against its own totals."""
    p = _Tables()
    p.feed(page)
    table = next((t for t in p.tables
                  if t and any("prize value" in c.lower() for c in t[0])), None)
    if not table:
        raise Refused("no prize table on the allocation page")
    head = [c.lower() for c in table[0]]
    # The current draw is the first "<Month> <Year> draw" column that is not
    # the next month's estimate.
    col = next((i for i, c in enumerate(head)
                if re.search(r"[a-z]+ \d{4} draw", c) and "estimate" not in c), None)
    vcol = next((i for i, c in enumerate(head) if "prize value" in c), None)
    if col is None or vcol is None:
        raise Refused(f"prize table header not recognised: {table[0]}")
    month, year = re.search(r"([a-z]+) (\d{4}) draw", head[col]).groups()
    if month not in MONTHS:
        raise Refused(f"draw month not recognised: {head[col]!r}")

    prizes, total_n, total_v = [], None, None
    for row in table[1:]:
        if len(row) <= col:
            continue
        label = row[0].lower()
        if "total prizes" in label:
            total_n = _count(row[col])
        elif "total value" in label:
            total_v = _money(row[col])
        else:
            value, n = _money(row[vcol]), _count(row[col])
            if value is None or n is None:
                raise Refused(f"unreadable prize row: {row}")
            prizes.append({"value": value, "count": n})

    values = [x["value"] for x in prizes]
    if len(prizes) < 5:
        raise Refused(f"only {len(prizes)} prize rows found")
    if values != sorted(set(values), reverse=True):
        raise Refused(f"prize values are not distinct and descending: {values}")
    if any(x["count"] <= 0 for x in prizes):
        raise Refused("a prize value has no prizes")
    if total_n is None or total_v is None:
        raise Refused("the table's total rows are missing")
    got_n = sum(x["count"] for x in prizes)
    got_v = sum(x["count"] * x["value"] for x in prizes)
    if got_n != total_n:
        raise Refused(f"counts sum to {got_n:,}, the table says {total_n:,} prizes")
    if got_v != total_v:
        raise Refused(f"prizes are worth £{got_v:,}, the table says £{total_v:,}")
    return {"label": f"{month.title()} {year}",
            "asOf": f"{year}-{MONTHS[month]:02d}",
            "prizes": prizes, "totalPrizes": total_n, "totalValue": total_v}


# ---------- the rate and the odds ----------

def _text(page: str) -> str:
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).replace("\xa0", " ")


def _current(text: str, pattern: str) -> list[str]:
    """Every match of `pattern` not qualified as history ("until ...")."""
    out = []
    for m in re.finditer(pattern, text, re.I):
        tail = text[m.end():m.end() + 70].lower()
        if "until" not in tail.split("from")[0]:
            out.append(m.group(1))
    return out


def parse_product(page: str) -> dict:
    text = _text(page)
    rates = _current(text, r"(\d+(?:\.\d+)?)%,? variable")
    odds = _current(text, r"(\d[\d,]*) to 1 for every £1 Bond")
    if not rates:
        raise Refused("no prize fund rate on the product page")
    if not odds:
        raise Refused("no odds on the product page")
    if len(set(rates)) > 1:
        raise Refused(f"the product page states more than one current rate: {sorted(set(rates))}")
    if len(set(odds)) > 1:
        raise Refused(f"the product page states more than one current odds: {sorted(set(odds))}")
    return {"prizeFundRate": float(rates[0]), "oddsPerBond": int(odds[0].replace(",", ""))}


# ---------- the whole check ----------

def reconcile(alloc: dict, product: dict) -> dict:
    implied = 12 * alloc["totalValue"] / (alloc["totalPrizes"] * product["oddsPerBond"]) * 100
    if abs(implied - product["prizeFundRate"]) > RATE_TOLERANCE_PP:
        raise Refused(f"the pages disagree: the {alloc['label']} table at 1 in "
                      f"{product['oddsPerBond']:,} implies {implied:.3f}%, the product "
                      f"page says {product['prizeFundRate']}% - one of them is mid-change")
    return {**alloc, **product, "impliedRate": round(implied, 4)}


def build(alloc_page: str, product_page: str, stored: dict | None, today: date) -> dict:
    draw = reconcile(parse_allocation(alloc_page), parse_product(product_page))
    if stored and stored.get("asOf", "") > draw["asOf"]:
        raise Refused(f"the page shows the {draw['label']} draw, older than the "
                      f"{stored.get('label')} draw already stored")
    return {"label": draw["label"], "asOf": draw["asOf"],
            "prizeFundRate": draw["prizeFundRate"], "oddsPerBond": draw["oddsPerBond"],
            "totalPrizes": draw["totalPrizes"], "totalValue": draw["totalValue"],
            "impliedRate": draw["impliedRate"], "prizes": draw["prizes"],
            "checked": today.isoformat(),
            "sources": [ALLOCATION_URL, PRODUCT_URL]}


def _changes(old: dict | None, new: dict) -> list[str]:
    if not old:
        return ["new file"]
    out = []
    for k, name in (("label", "draw"), ("prizeFundRate", "rate"), ("oddsPerBond", "odds")):
        if old.get(k) != new[k]:
            out.append(f"{name} {old.get(k)} -> {new[k]}")
    if old.get("prizes") != new["prizes"]:
        out.append("prize counts")
    return out


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    stored = None
    if os.path.exists(OUT):
        stored = json.load(io.open(OUT, encoding="utf-8"))
    pages = []
    for url in (ALLOCATION_URL, PRODUCT_URL):
        r = netfetch.fetch(url, ua=netfetch.UA_BROWSER)
        if not r:
            print(f"  SKIP premium bonds: {url} could not be read ({r.error})")
            return 0
        pages.append(r.text)
    try:
        new = build(pages[0], pages[1], stored, date.today())
    except Refused as why:
        print(f"  SKIP premium bonds: {why}")
        return 0
    changes = _changes(stored, new)
    print(f"  OK   premium bonds: {new['label']} draw, {new['prizeFundRate']}%, "
          f"1 in {new['oddsPerBond']:,}, {new['totalPrizes']:,} prizes worth "
          f"£{new['totalValue']:,} - " + (", ".join(changes) if changes else "unchanged"))
    if not dry:
        with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
            json.dump(new, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return 0


# ---------- self-test ----------

def _alloc_fixture(rows=None, total_n="6,546,849", total_v="£498,378,775",
                   month="October 2026"):
    rows = rows or [("£1 million", "2"), ("£100,000", "96"), ("£50,000", "191"),
                    ("£25,000", "382"), ("£10,000", "957"), ("£5,000", "1,914"),
                    ("£1,000", "19,934"), ("£500", "59,802"), ("£100", "2,371,145"),
                    ("£50", "2,371,145"), ("£25", "1,721,281")]
    body = "".join(f"<tr><td>&nbsp;</td><td>{v}</td><td>{n}</td><td>1</td></tr>" for v, n in rows)
    return ("<table><thead><tr><th>Prize band</th><th>Prize value</th>"
            f"<th>{month} draw</th><th>Estimated November 2026 draw</th></tr></thead><tbody>"
            f"{body}<tr><td><strong>Total prizes</strong></td><td>&nbsp;</td><td>{total_n}</td><td>1</td></tr>"
            f"<tr><td><strong>Total value</strong></td><td>&nbsp;</td><td>{total_v}</td><td>1</td></tr>"
            "</tbody></table>")


PRODUCT_FIXTURE = (
    "<p>What's the annual prize fund rate? <!-- x -->4.35%, variable</p>"
    "<p>What are the odds of winning? 21,000 to 1 for every &pound;1 Bond in the monthly prize draw, variable.</p>"
    "<p>Annual prize fund rate 3.30%, variable (until the June 2026 prize draw). "
    "4.35%, variable (from the July 2026 prize draw).</p>"
    "<p>What are the odds of winning? 23,000 to 1 for every &pound;1 Bond in the monthly prize draw, "
    "variable (until the June 2026 prize draw). 21,000 to 1 for every &pound;1 Bond (from July).</p>")


def _refuses(fn, *args) -> str:
    try:
        fn(*args)
    except Refused as why:
        return str(why)
    raise AssertionError(f"{fn.__name__} should have refused")


def _selftest() -> None:
    today = date(2026, 10, 14)
    good = build(_alloc_fixture(), PRODUCT_FIXTURE, None, today)
    assert good["label"] == "October 2026" and good["asOf"] == "2026-10"
    assert good["prizes"][0] == {"value": 1_000_000, "count": 2}
    assert good["oddsPerBond"] == 21000 and good["prizeFundRate"] == 4.35
    assert abs(good["impliedRate"] - 4.35) < 0.001
    print("  parse + reconcile OK", file=sys.stderr)

    # Gate 1: a miscounted row breaks the totals.
    assert "the table says" in _refuses(parse_allocation, _alloc_fixture(total_n="6,546,850"))
    assert "worth" in _refuses(parse_allocation, _alloc_fixture(total_v="£498,378,776"))
    print("  totals gate      OK", file=sys.stderr)

    # Gate 2: the "until" notices are history, and two live figures refuse.
    assert parse_product(PRODUCT_FIXTURE)["oddsPerBond"] == 21000
    two = PRODUCT_FIXTURE + "<p>22,000 to 1 for every &pound;1 Bond.</p>"
    assert "more than one current odds" in _refuses(parse_product, two)
    assert "no odds" in _refuses(parse_product, "<p>4.35%, variable</p>")
    print("  history gate     OK", file=sys.stderr)

    # Gate 3: odds from a different month than the table.
    moved = PRODUCT_FIXTURE.replace("21,000 to 1", "22,000 to 1")
    assert "the pages disagree" in _refuses(build, _alloc_fixture(), moved, None, today)
    print("  agreement gate   OK", file=sys.stderr)

    # Gate 4: never step back a month.
    newer = {"asOf": "2026-11", "label": "November 2026"}
    assert "older than" in _refuses(build, _alloc_fixture(), PRODUCT_FIXTURE, newer, today)
    print("  no-rollback gate OK", file=sys.stderr)
    print("  premium_bonds self-test: OK", file=sys.stderr)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
