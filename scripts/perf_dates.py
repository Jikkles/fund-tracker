"""
Read the measurement date out of a performance table's own period labels.

WHY THIS EXISTS
---------------
`performance.perfAsAt` is consumed in three places as "the date these figures
are measured to": the run's caveat block, research_health(), and the page's
staleness readout. It used to be *written* as "the date we last fetched the
factsheet" - hl_factsheet.py stamped today on every successful read, including
reads that deliberately KEPT the stored discrete table rather than replacing
it, because the stored one carries sector comparators HL does not publish.

The two meanings drifted apart and the second one won, with three results:

  * the caveat printed "the tables are NOT refreshed by the daily run: as-at
    dates are all as at 3 Sep 2026, up to 0 days old" - a sentence that
    contradicts itself in its own second clause;
  * research_health() could never fire. It warns past 120 days; the weekly
    factsheet run reset every stamp to today, so no fund could ever reach 8;
  * a table measured to 31 Mar 2026 was published as current.

A confirmation date is a real and useful thing - it is what fund["asAt"] and
performance["perfConfirmed"] hold. It is simply not a measurement date, and
using one as the other is the "never invent a figure" rule broken by a date
instead of a number.

WHERE THE DATE COMES FROM
-------------------------
The tables already carry it. Every period label states the window it covers -
"1 yr (trailing, to 26 Jun 26)", "02/09/25 to 02/09/26", "31 Mar 25 - 31 Mar
26" - so the measurement date is read from the data rather than assumed. A
label that states no date yields nothing, and a table of such labels has no
known as-at: absent, not today.
"""

from __future__ import annotations

import re
from datetime import date

# A parsed date beyond this many days ahead of the run is a misread label, not
# a real future measurement, and is refused. cb_calendar.py refuses
# out-of-horizon dates the same way and for the same reason.
FUTURE_SLACK_DAYS = 3

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# "26 Jun 26", "20 May 2026", "31 Mar 25" - day, month name, 2- or 4-digit year.
_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{2}|\d{4})\b")
# "02/09/26" - HL's discrete table. Day first: these are UK factsheets, and the
# rows run 02/09/21..02/09/26, which no month-first reading survives.
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")
# "to Dec 2025" - a month with no day.
_MY = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b")
# "Since Jan 2012 (manager tenure)", "Since launch Mar 2016" - the date in a
# since-label is where the window STARTS. Its end is "now", and the label does
# not say which now, so the row states no measurement date at all. Read as an
# end date it dated one fund's tables to 2012 and put it 5,359 days stale.
_SINCE = re.compile(r"\bsince\b", re.I)


def _year(raw: str) -> int:
    """Two-digit years are this century. The horizon check catches the rest."""
    n = int(raw)
    return n if n >= 1000 else 2000 + n


def _make(day: int, month: int, year: int, today: date) -> date | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    return d if (d - today).days <= FUTURE_SLACK_DAYS else None


def period_end(label: str | None, today: date | None = None) -> date | None:
    """The date one period label says it is measured to, or None.

    The LAST date in the label, not the first: every dated form here is either
    a single "to <date>" or a "<start> to <end>" range, and both end on the
    date that matters. A label with no date at all - "2025-26", "3 yr
    (derived)", "Since launch (2012)" - yields None rather than a guess.
    """
    if not label or _SINCE.search(label):
        return None
    today = today or date.today()
    found: list[date] = []

    for day, mon, yr in _DMY.findall(label):
        month = MONTHS.get(mon.lower())
        if month and (d := _make(int(day), month, _year(yr), today)):
            found.append(d)
    for day, mon, yr in _SLASH.findall(label):
        if (d := _make(int(day), int(mon), _year(yr), today)):
            found.append(d)
    if found:
        return max(found)

    # A month with no day. The first of the month, not the last: for a
    # staleness measure the conservative reading is the older one, and
    # inventing the 31st to look fresher is the wrong direction to be wrong in.
    for mon, yr in _MY.findall(label):
        month = MONTHS.get(mon.lower())
        if month and (d := _make(1, month, int(yr), today)):
            found.append(d)
    return max(found) if found else None


def table_as_at(rows: list[dict] | None, key: str,
                today: date | None = None) -> date | None:
    """The newest period end in one table - what the table is measured to."""
    if not rows:
        return None
    ends = [d for r in rows if (d := period_end(r.get(key), today))]
    return max(ends) if ends else None


def perf_as_at(perf: dict | None,
               today: date | None = None) -> tuple[date | None, str]:
    """(measurement date, source note) for a fund's performance tables.

    The OLDER of the cumulative and discrete tables where both carry a date.
    They are frequently on different bases and different vintages - a fund can
    hold an HL discrete table scraped this morning beside a cumulative table
    researched in June - and the pair is only as current as its older half.
    Taking the newer would let a fresh table vouch for a stale one sitting
    directly beneath it on the same card, which is the exact failure this
    module exists to end.
    """
    if not perf:
        return None, ""
    today = today or date.today()
    dated = {"cumulative": table_as_at(perf.get("cumulative"), "period", today),
             "discrete": table_as_at(perf.get("discrete"), "year", today)}
    have = {k: v for k, v in dated.items() if v}
    if not have:
        return None, ""
    note = ("oldest of the cumulative and discrete tables" if len(have) == 2
            else f"the {next(iter(have))} table")
    return min(have.values()), f"period labels ({note})"


def stamp(fund: dict, today: date | None = None) -> bool:
    """Set performance.perfAsAt from the fund's own period labels.

    Returns True if anything changed. Called from the daily run as well as the
    weekly factsheet refresh, so a fund whose stored stamp predates this module
    is corrected on the next run rather than on the next scrape.

    Where no table carries a date the field is REMOVED, not left holding the
    old wrong value and not set to today. research_health() skips a fund with
    no perfAsAt, which is the correct behaviour for "we do not know how old
    this is" - an unknown age cannot be reported as a number of days.
    """
    perf = fund.get("performance")
    if not perf:
        return False
    got, note = perf_as_at(perf, today)
    before = (perf.get("perfAsAt"), perf.get("perfAsAtSource"))
    if got:
        perf["perfAsAt"] = got.isoformat()
        perf["perfAsAtSource"] = note
    else:
        perf.pop("perfAsAt", None)
        perf.pop("perfAsAtSource", None)
    return before != (perf.get("perfAsAt"), perf.get("perfAsAtSource"))


def _selftest() -> bool:
    today = date(2026, 9, 3)

    def pe(s):
        return period_end(s, today)

    # The dated forms actually present in the data.
    assert pe("1 yr (trailing, to 26 Jun 26)") == date(2026, 6, 26)
    assert pe("02/09/25 to 02/09/26") == date(2026, 9, 2)
    assert pe("31 Mar 25 – 31 Mar 26") == date(2026, 3, 31)
    assert pe("30 Jun 25 – 30 Jun 26") == date(2026, 6, 30)
    assert pe("2025–26 (to 31 Mar 26)") == date(2026, 3, 31)
    assert pe("3 yr annualised (Fidelity, to 07 Jul 26)") == date(2026, 7, 7)
    assert pe("1 yr (trailing, Fidelity price stamp 8 May 26 — as-at "
              "approximate)") == date(2026, 5, 8)
    assert pe("1 yr (to ~20 May 2026, FE Analytics)") == date(2026, 5, 20)
    assert pe("1 yr (to 16 Jun 26, OEIC)") == date(2026, 6, 16)
    assert pe("1 yr (to 24 Jun 2026, HL rolling)") == date(2026, 6, 24)

    # A month with no day reads as the 1st - the older end, deliberately.
    assert pe("10 yr (to Dec 2025)") == date(2025, 12, 1)

    # A since-label states a start, not a measurement date.
    assert pe("Since Jan 2012 (manager tenure, cumulative)") is None
    assert pe("Since launch Mar 2016 (cumulative)") is None
    assert pe("Since strategy launch (2012)") is None

    # Undated labels yield nothing. A bare "2025-26" is the most common
    # discrete label on the desk and states no measurement date whatsoever;
    # reading 26 out of it as a year would date the table to Jan 2026 on the
    # strength of a hyphen.
    for undated in ("2025–26", "2023–24", "3 yr", "5 yr annualised",
                    "3 yr (derived)", "Since launch (2012)",
                    "1 yr (Fidelity/Morningstar)",
                    "1 yr (2025–26, HL rolling)", "", None):
        assert pe(undated) is None, undated

    # A label that parses to the future is a misread, not a measurement.
    assert pe("1 yr (to 26 Jun 27)") is None
    assert pe("31/12/99") is None
    # ...but the run's own day is real.
    assert pe("to 03 Sep 26") == date(2026, 9, 3)

    # A range takes its end, not its start.
    assert pe("02/09/21 to 02/09/22") == date(2022, 9, 2)

    # The table is measured to its newest period.
    rows = [{"year": "02/09/25 to 02/09/26"}, {"year": "02/09/21 to 02/09/22"}]
    assert table_as_at(rows, "year", today) == date(2026, 9, 2)
    assert table_as_at([{"year": "2025–26"}], "year", today) is None
    assert table_as_at([], "year", today) is None
    assert table_as_at(None, "year", today) is None

    # A fresh discrete table does not vouch for a stale cumulative one.
    got, note = perf_as_at({
        "cumulative": [{"period": "1 yr (trailing, to 26 Jun 26)"}],
        "discrete": [{"year": "02/09/25 to 02/09/26"}]}, today)
    assert got == date(2026, 6, 26), got
    assert "oldest" in note

    # One dated table is enough, and the note says which one carried it.
    got, note = perf_as_at({
        "cumulative": [{"period": "3 yr (derived)"}],
        "discrete": [{"year": "2025–26 (to 31 Mar 26)"}]}, today)
    assert got == date(2026, 3, 31) and "discrete" in note, (got, note)

    # No dated table anywhere is an absent as-at, not today's date.
    assert perf_as_at({"cumulative": [{"period": "3 yr"}],
                       "discrete": [{"year": "2024–25"}]}, today) == (None, "")
    assert perf_as_at({}, today) == (None, "")
    assert perf_as_at(None, today) == (None, "")

    # stamp() corrects a stored stamp rather than trusting it...
    f = {"performance": {"perfAsAt": "2026-09-03",
                         "perfAsAtSource": "HL factsheet scrape",
                         "discrete": [{"year": "2025–26 (to 31 Mar 26)"}]}}
    assert stamp(f, today) is True
    assert f["performance"]["perfAsAt"] == "2026-03-31"
    assert "period labels" in f["performance"]["perfAsAtSource"]
    # ...is idempotent once corrected...
    assert stamp(f, today) is False

    # ...and removes the field outright where nothing carries a date, rather
    # than leaving today's date standing in for an unknown one.
    g = {"performance": {"perfAsAt": "2026-09-03",
                         "discrete": [{"year": "2024–25"}]}}
    assert stamp(g, today) is True
    assert "perfAsAt" not in g["performance"]
    assert stamp({}, today) is False

    print("  period labels    OK  (dated forms read, undated ones refused, "
          "future dates rejected)")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
