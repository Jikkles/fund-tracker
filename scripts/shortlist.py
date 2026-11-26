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
from datetime import date
from pathlib import Path

import datafile
import netfetch

FUNDS = Path(__file__).resolve().parent.parent / "data" / "funds.json"
URL = "https://www.hl.co.uk/ajax/funds/wealth-150/all-data"
USER_AGENT = netfetch.UA_BROWSER
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

# A read this small is a failed read, not a Shortlist. HL's list has run at
# 70-odd entries; a feed that changed shape, or a page of nothing, comes back
# far below that, and scoring every fund against it would mark the whole desk
# Ex-Shortlist and publish it.
MIN_ENTRIES = 30
# HL reviews the list and drops funds a few at a time. A run that would drop
# more than this share of the funds currently on it is far likelier to be a
# matching failure - a slug scheme change, a SEDOL field renamed - than a
# decision HL made, so it is refused rather than written.
MAX_DROP_SHARE = 0.5


def refusal(entries: int, was_on: int, dropping: int) -> str | None:
    """Why this read must not be written, or None if it can be.

    The module docstring always said `main` refuses an empty read. It never
    did: an empty feed scored every fund against nothing, every Shortlist
    badge flipped to Ex-Shortlist, the script exited 0, and the daily run
    would have committed and published it.
    """
    if entries < MIN_ENTRIES:
        return (f"HL's feed returned {entries} entries, under the {MIN_ENTRIES} "
                f"a real Shortlist has - a failed read, not a list")
    if was_on and dropping > was_on * MAX_DROP_SHARE:
        return (f"this read would drop {dropping} of the {was_on} funds on the "
                f"Shortlist at once - a matching failure, not an HL decision")
    return None


def fetch() -> list[dict]:
    """HL's own Shortlist feed. Empty on failure - never a partial list.

    This ran with no retry: one blip and every fund on the desk was scored
    against an empty list. `main` refuses to write on an empty read for that
    reason, and netfetch now retries before it comes to that.
    """
    doc = netfetch.fetch_json(URL, ua=USER_AGENT, timeout=TIMEOUT, headers={
        "Accept": "application/json",
        "Referer": "https://www.hl.co.uk/funds/help-choosing-funds/wealth-shortlist",
    })
    if not isinstance(doc, dict):
        return []
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
    why = refusal(len(rows), 0, 0)
    if why:
        print(f"[refused] {why}. funds.json left exactly as it was.",
              file=sys.stderr)
        return 1

    doc = json.load(io.open(FUNDS, encoding="utf-8"))
    changes, on, off = [], 0, 0
    was_on = dropping = 0
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
        was_on += was == "on"
        dropping += was == "on" and now == "dropped"
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
    # Regenerated every run. Written once by hand, it said "as at 2 Sep 2026"
    # for three months of daily re-reads.
    today = date.today()
    doc.setdefault("meta", {})["shortlistNote"] = (
        f"{on} of {on + off} funds are on HL's Wealth Shortlist as at "
        f"{today.day} {today:%b %Y}, read from HL's own Wealth Shortlist data. "
        "The rest are carried deliberately: funds HL has dropped but the desk "
        "still tracks, and funds covering asset classes the Shortlist does not "
        "include at all.")
    if changes:
        print("\nchanged:")
        print("\n".join(changes))
    else:
        print("\nno status changed")
    why = refusal(len(rows), was_on, dropping)
    if why:
        print(f"\n[refused] {why}. funds.json left exactly as it was.",
              file=sys.stderr)
        return 1
    if dry:
        print("\n--dry-run: nothing written")
        return 0
    datafile.write_json(FUNDS, doc)
    print(f"\nwrote {FUNDS}")
    return 0


def _selftest() -> bool:
    # A real day: 72 entries, a couple of funds dropped. Written.
    assert refusal(72, 69, 2) is None
    # The failures this exists for: nothing back, or a fraction of a list.
    assert "failed read" in refusal(0, 0, 0)
    assert "failed read" in refusal(MIN_ENTRIES - 1, 69, 0)
    # A full-size feed that matches nothing - renamed fields, new slugs.
    assert "matching failure" in refusal(72, 69, 69)
    assert "matching failure" in refusal(72, 69, 35)
    assert refusal(72, 69, 34) is None, "half the list is still a review"
    # A desk with nothing on the list yet cannot divide by it.
    assert refusal(72, 0, 0) is None
    print("  refusal          OK  (empty/short feed and mass drop refused)")

    assert slug_key(slug("https://www.hl.co.uk/funds/fund-discounts,-prices"
                         "--and--factsheets/search-results/a/"
                         "artemis-income-class-i-accumulation")) == \
        slug_key("artemis-income-i-acc")
    print("  shortlist self-test: OK")
    return True


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
