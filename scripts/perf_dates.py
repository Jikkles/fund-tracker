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

TWO TABLES, TWO CLOCKS
----------------------
They do not age the same way, and treating them alike over-reported staleness
badly on the first cut of this module.

A CUMULATIVE table is trailing: "1 yr (trailing, to 26 Jun 26)" claims to be
the last twelve months, and every day that passes makes it less so. It ages by
the calendar, and days-since-measured is the right measure of it.

A DISCRETE table is a run of completed annual periods on a fixed basis - every
dated one on this desk is strictly consistent, five consecutive years ending
the same month and day: 2 Sep for the 67 scraped from HL, 31 Mar for five,
30 Jun for three. The year to 31 Mar 2026 is a historical fact. It does not
become less true in September, and the next period on that basis does not
close until 31 Mar 2027, so nothing is missing from the table at all. Aged by
the calendar it read as 156 days stale and put fourteen funds on a worklist
with nothing to do. What can genuinely go wrong is a period completing and not
being added - so a discrete table is measured in MISSING YEARS, not in days.
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
    """(measurement date, source note) for a fund's TRAILING figures.

    The cumulative table only. A trailing figure is a claim about the last
    twelve months, or three years, or five, and it decays from the day it is
    struck - so the day it was struck is the thing worth knowing.

    The discrete table is deliberately not folded in here. It is a run of
    completed annual periods, and a completed year does not decay; see
    discrete_status() for the check that does apply to it. An earlier cut of
    this function took the older of the two, which meant a complete and
    perfectly good 31 Mar 2026 annual table dragged its fund onto a staleness
    worklist 156 days later with nothing whatsoever to re-research.
    """
    if not perf:
        return None, ""
    got = table_as_at(perf.get("cumulative"), "period", today or date.today())
    return (got, "period labels (cumulative table)") if got else (None, "")


def _add_year(d: date) -> date:
    """The same day next year, stepping back off 29 February."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def discrete_status(rows: list[dict] | None,
                    today: date | None = None) -> dict | None:
    """How a discrete annual table stands, or None if it cannot be judged.

    Returns the basis it is kept on, the newest period it carries, and how
    many further annual periods have closed since without being added. A table
    whose newest year is the newest year that has closed is complete, however
    many months ago that was.
    """
    if not rows:
        return None
    today = today or date.today()
    ends = sorted({e for r in rows if (e := period_end(r.get("year"), today))})
    if not ends:
        return None
    newest = ends[-1]
    # Only call it a fixed annual basis when the table really is one. Every
    # dated discrete table on the desk is - five consecutive years ending the
    # same month and day - but a table that is not cannot have its next period
    # predicted, so it reports the fact rather than a guess.
    consistent = all((e.month, e.day) == (newest.month, newest.day)
                     for e in ends)
    missing, cur = 0, newest
    while _add_year(cur) <= today:
        cur = _add_year(cur)
        missing += 1
    return {"basis": f"{newest:%d %b}", "newest": newest,
            "consistent": consistent, "missing": missing,
            "nextDue": _add_year(newest)}


FIELDS = ("perfAsAt", "perfAsAtSource", "discreteAsAt", "discreteBasis",
          "discreteMissingYears")


def stamp(fund: dict, today: date | None = None) -> bool:
    """Date a fund's tables from their own period labels. True if changed.

    Called from the daily run as well as the weekly factsheet refresh, so a
    fund whose stored stamp predates this module is corrected on the next run
    rather than on the next scrape.

    perfAsAt dates the trailing (cumulative) figures. discreteAsAt records the
    newest annual period the discrete table carries, with the basis it is kept
    on and how many further years have closed without being added - which is
    what "out of date" means for an annual table, and is almost always zero.

    Any field the tables cannot support is REMOVED, not left holding an old
    value and not set to today. An unknown age cannot be reported as a number.
    """
    perf = fund.get("performance")
    if not perf:
        return False
    before = tuple(perf.get(k) for k in FIELDS)

    got, note = perf_as_at(perf, today)
    if got:
        perf["perfAsAt"], perf["perfAsAtSource"] = got.isoformat(), note
    else:
        perf.pop("perfAsAt", None)
        perf.pop("perfAsAtSource", None)

    dis = discrete_status(perf.get("discrete"), today)
    if dis:
        perf["discreteAsAt"] = dis["newest"].isoformat()
        perf["discreteMissingYears"] = dis["missing"]
        if dis["consistent"]:
            perf["discreteBasis"] = dis["basis"]
        else:
            perf.pop("discreteBasis", None)
    else:
        for key in ("discreteAsAt", "discreteBasis", "discreteMissingYears"):
            perf.pop(key, None)

    return before != tuple(perf.get(k) for k in FIELDS)


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

    # The cumulative table alone sets the trailing clock. A fresh discrete
    # table beside a stale cumulative one does not rescue it...
    got, note = perf_as_at({
        "cumulative": [{"period": "1 yr (trailing, to 26 Jun 26)"}],
        "discrete": [{"year": "02/09/25 to 02/09/26"}]}, today)
    assert got == date(2026, 6, 26), got
    assert "cumulative" in note

    # ...and a complete annual table on its own does not set one at all. This
    # is the case that mattered: fourteen funds carry a 31 Mar discrete table
    # and no cumulative table, and taking the older of the two put every one
    # of them 156 days stale with nothing to re-research.
    assert perf_as_at({"discrete": [{"year": "2025–26 (to 31 Mar 26)"}]},
                      today) == (None, "")
    assert perf_as_at({"cumulative": [{"period": "3 yr (derived)"}]},
                      today) == (None, "")
    assert perf_as_at({}, today) == (None, "")
    assert perf_as_at(None, today) == (None, "")

    # --- the discrete table's own clock: missing years, not days ---------
    def ds(labels, when=today):
        return discrete_status([{"year": l} for l in labels], when)

    # A 31 Mar table in September is complete: the year to 31 Mar 2026 has
    # closed and been added, and the next does not close until 31 Mar 2027.
    st = ds(["2025–26 (to 31 Mar 26)", "2024–25 (to 31 Mar 25)"])
    assert st["missing"] == 0 and st["consistent"], st
    assert st["nextDue"] == date(2027, 3, 31) and st["basis"] == "31 Mar"

    # HL's 2 Sep basis, one day after the newest period closed.
    assert ds(["02/09/25 to 02/09/26", "02/09/24 to 02/09/25"])["missing"] == 0

    # A year that closed and was never added is what "out of date" means here.
    assert ds(["2024–25 (to 31 Mar 25)"])["missing"] == 1
    assert ds(["2021–22 (to 31 Mar 22)"])["missing"] == 4

    # A table that is not on one fixed basis cannot have its next period
    # predicted, and says so rather than guessing.
    assert ds(["to 31 Mar 26", "to 30 Jun 25"])["consistent"] is False

    # Undated and empty tables yield nothing.
    assert ds(["2025–26", "2024–25"]) is None
    assert discrete_status([], today) is None
    assert discrete_status(None, today) is None

    # stamp() corrects a stored stamp rather than trusting it...
    f = {"performance": {"perfAsAt": "2026-09-03",
                         "perfAsAtSource": "HL factsheet scrape",
                         "cumulative": [{"period": "1 yr (to 30 Apr 26)"}],
                         "discrete": [{"year": "2025–26 (to 31 Mar 26)"}]}}
    assert stamp(f, today) is True
    assert f["performance"]["perfAsAt"] == "2026-04-30"
    assert f["performance"]["discreteAsAt"] == "2026-03-31"
    assert f["performance"]["discreteMissingYears"] == 0
    assert f["performance"]["discreteBasis"] == "31 Mar"
    # ...and is idempotent once corrected.
    assert stamp(f, today) is False

    # A discrete-only fund carries no trailing clock at all - the 46 funds
    # this describes have nothing that decays by the calendar.
    g = {"performance": {"perfAsAt": "2026-09-03",
                         "discrete": [{"year": "02/09/25 to 02/09/26"}]}}
    assert stamp(g, today) is True
    assert "perfAsAt" not in g["performance"]
    assert g["performance"]["discreteMissingYears"] == 0
    assert stamp({}, today) is False

    print("  period labels    OK  (dated forms read, undated ones refused, "
          "future dates rejected)")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
