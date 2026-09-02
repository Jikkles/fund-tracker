"""
Set each fund's Wealth Shortlist status from HL's own published list.

WHY THIS IS NOT SCRAPED FROM THE FACTSHEET
------------------------------------------
It used to be. hl_factsheet.py looked for the sentence "our analysts have
selected this fund for the Wealth Shortlist" in the factsheet text, and set
the badge from that. HL ship that sentence six times in a hidden tooltip
template on every fund page, shortlisted or not - so the test returned True
for every fund the desk has ever read, and 68 of 70 funds carried a
"Wealth Shortlist" badge that carried no information at all.

HL publish the Shortlist as a list, at the endpoint their own Wealth Shortlist
page reads. So read the list. It carries a SEDOL and a factsheet link per
entry, both of which are exact identifiers - no name matching, no floor, no
heuristic standing in for a person.

WHAT THE BADGE MEANS
--------------------
  Wealth Shortlist   on HL's list as at this run
  Ex-Shortlist       tracked here, dropped by HL
  (no badge)         never on it - the asset classes HL does not shortlist
                     at all, such as property, infrastructure, commodities,
                     index-linked gilts and cash

It is HL's view, not a rating by this desk, and the caveat on the page says
so. A fund leaving the Shortlist is a fact worth showing, not a reason to
stop tracking it.
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
from pathlib import Path

FUNDS = Path(__file__).resolve().parent.parent / "data" / "funds.json"
URL = "https://www.hl.co.uk/ajax/funds/wealth-150/all-data"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
TIMEOUT = 30

ON = {"type": "", "label": "Wealth Shortlist"}
OFF = {"type": "off", "label": "Ex-Shortlist"}
# "never" deliberately maps to no badge at all.
BADGE = {"on": ON, "dropped": OFF, "never": None}

# Slug words that identify a share class or a document type rather than a
# fund, dropped before two slugs are compared.
DROP = {"class", "accumulation", "income", "acc", "inc", "hedged", "gbp",
        "professional", "fd", "trust", "fund", "and", "the", "of", "oeic",
        "monthly", "quarterly", "gross", "update"}
MONTHS = ("january|february|march|april|may|june|july|august|september"
          "|october|november|december")


def fetch() -> list[dict]:
    req = urllib.request.Request(URL, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.hl.co.uk/funds/help-choosing-funds/wealth-shortlist",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        doc = json.loads(r.read().decode("utf-8", "replace"))
    rows = (doc.get("data") or {}).get("fundData") or []
    # One row in the feed is an empty template with no SEDOL. Drop it rather
    # than letting it match a fund whose identifiers are also missing.
    return [f for f in rows if f.get("sedol")]


def slug(url: str | None) -> str:
    m = re.search(r"/([a-z0-9.\-]+)/?$", (url or "").strip().lower())
    return m.group(1) if m else ""


def slug_key(s: str) -> frozenset:
    """Share-class-insensitive key for a factsheet slug."""
    s = re.sub(rf"-(?:{MONTHS})-\d{{4}}-fund-update$", "", s)
    s = re.sub(r"-fund-update$", "", s)
    return frozenset(w for w in s.replace(".", "").split("-")
                     if w and w not in DROP and not (len(w) == 1 and w.isalpha()))


def sedol_of(fund: dict) -> str | None:
    """A UK fund ISIN carries its SEDOL at positions 4-10."""
    if isinstance(fund.get("sedol"), str) and fund["sedol"]:
        return fund["sedol"]
    isin = fund.get("isin")
    if isinstance(isin, str) and len(isin) == 12:
        return isin[4:11]
    return None


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    rows = fetch()
    by_sedol = {r["sedol"]: r for r in rows}
    by_key: dict[frozenset, dict] = {}
    for r in rows:
        k = slug_key(slug(r.get("factsheetLink")))
        if k:
            by_key.setdefault(k, r)
    print(f"HL Wealth Shortlist: {len(rows)} entries, "
          f"{len(by_key)} distinct funds")

    doc = json.load(io.open(FUNDS, encoding="utf-8"))
    changes, on, off = [], 0, 0
    for f in doc["funds"]:
        sed = sedol_of(f)
        hit = by_sedol.get(sed) if sed else None
        how = "SEDOL"
        if hit is None:
            hit = by_key.get(slug_key(slug((f.get("links") or {}).get("hl"))))
            how = "slug"
        was = f.get("shortlistStatus")
        # "never" is sticky until HL adds the fund: a property or money-market
        # fund is not "dropped" from a list it was never on, and saying so
        # would read as a downgrade HL never made.
        now = "on" if hit is not None else ("never" if was == "never" else "dropped")
        f["shortlistStatus"] = now
        f["shortlist"] = now == "on"
        on, off = on + (now == "on"), off + (now != "on")
        want = BADGE.get(now)
        have = (f.get("badge") or {}).get("label")
        if want is None:
            # never on the list: leave whatever badge the fund already carries,
            # but never leave a Shortlist badge standing on it.
            if have in (ON["label"], OFF["label"]):
                changes.append(f"  {f['name'][:46]:46} {have!r} -> (no shortlist badge)")
                f.pop("badge", None)
        elif have != want["label"]:
            changes.append(f"  {f['name'][:46]:46} {have!r} -> {want['label']!r} ({how})")
            f["badge"] = dict(want)
        if was and was != now:
            changes.append(f"  {f['name'][:46]:46} status {was} -> {now} ({how})")

    print(f"{on} on the Shortlist, {off} not")
    if changes:
        print("\nchanged:")
        print("\n".join(changes))
    else:
        print("\nno status changed")
    if dry:
        print("\n--dry-run: nothing written")
        return 0
    io.open(FUNDS, "w", encoding="utf-8", newline="\n").write(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {FUNDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
