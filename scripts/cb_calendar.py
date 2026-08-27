"""
Central bank meeting dates, read from the published calendars.

These used to be typed into calendar_data.py by hand, which worked until it
did not: the table ran out, the run warned about it for weeks, and an
exhausted table means the rate catalyst silently drops off the page. This
module reads the same two pages a human would.

  FOMC   federalreserve.gov/monetarypolicy/fomccalendars.htm
  BoE    bankofengland.co.uk/monetary-policy/upcoming-mpc-dates

The pages are scraped, so the parse is the dangerous step - a regex that
half-matches yields a confident, precise, wrong date wearing a (confirmed)
label, which is worse than an honestly exhausted table. Everything here is
built to fail closed instead:

  * A year is accepted only if it yields a plausible number of meetings.
    Both committees meet eight times a year; a panel parsing to two means
    the markup moved, not that the Fed cancelled six meetings.
  * Anything not understood is dropped rather than guessed at. The Fed lists
    the occasional "22 (notation vote)" among the scheduled meetings; a
    notation vote is not a policy decision and is not published as one.
  * Dates far outside a sane horizon are refused, which catches a parse that
    has locked onto the wrong number entirely.
  * A whole-page failure returns None, distinct from an empty list, so the
    caller can tell "could not read" from "read it, nothing there".

BoE PROVISIONAL DATES ARE NOT CONFIRMED DATES. The Bank publishes one year
under "confirmed dates" and the next under "provisional dates", and that
distinction is the page's own word, not an inference. It is carried through
to the label - marking a provisional date (confirmed) is the exact failure
the confirmed/estimated split exists to prevent.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from market_data import USER_AGENT, TIMEOUT

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BOE_URL = "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates"

# A committee that meets eight times a year should never parse to fewer than
# this. Below it, assume the markup moved rather than that meetings vanished.
MIN_MEETINGS_PER_YEAR = 6

# Sanity horizon. Neither bank publishes more than a couple of years ahead,
# and a date outside this window is a parse that has gone wrong.
HORIZON_YEARS_BACK = 2
HORIZON_YEARS_FORWARD = 3

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
# The Fed abbreviates the month cell on meetings that straddle a boundary.
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})


@dataclass(frozen=True)
class Meeting:
    """One rate decision. `status` is the publisher's own word for it."""
    day: date
    status: str          # "confirmed" | "provisional"

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


def _plausible(d: date, today: date) -> bool:
    return (date(today.year - HORIZON_YEARS_BACK, 1, 1) <= d
            <= date(today.year + HORIZON_YEARS_FORWARD, 12, 31))


def _fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# FOMC
# ---------------------------------------------------------------------------
# Each year is a panel headed "<YYYY> FOMC Meetings", holding one row per
# meeting: a month cell and a date cell. A two-day meeting reads "27-28" and
# the decision lands on the second day. A meeting straddling a month end
# reads month "Apr/May", date "30-1" - so the decision day belongs to the
# SECOND month, which is why the month cell has to be split rather than read.
_FOMC_PANEL = re.compile(r'<div class="panel panel-default">')
# Whitespace-tolerant throughout: the Fed's markup is machine-generated and
# reflows, and a parser that breaks on a newline is a parser that breaks.
_FOMC_YEAR = re.compile(
    r'<h4>\s*<a[^>]*>\s*(\d{4})\s+FOMC\s+Meetings', re.I)
_FOMC_MONTH = re.compile(
    r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>')
_FOMC_DATE = re.compile(r'fomc-meeting__date[^>]*>([^<]*)</div>')
# "27-28", "8-9*", "30-1", "22". A trailing * marks a Summary of Economic
# Projections meeting and carries no date information.
_FOMC_DAYS = re.compile(r'^(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\*?$')


def parse_fomc(html: str, today: date) -> list[Meeting] | None:
    """
    Scheduled FOMC decision dates.

    The Fed's own footnote says each date is tentative until confirmed at the
    preceding meeting. That applies equally to next month's meeting and to
    2027's, so treating it as disqualifying would mean no Fed date is ever
    confirmed. These are the published schedule and are labelled as such.
    """
    if not html:
        return None
    out: list[Meeting] = []
    for panel in _FOMC_PANEL.split(html):
        year_m = _FOMC_YEAR.search(panel)
        if not year_m:
            continue
        year = int(year_m.group(1))
        months = [m.strip() for m in _FOMC_MONTH.findall(panel)]
        days = [d.strip() for d in _FOMC_DATE.findall(panel)]
        if len(months) != len(days):
            # The two columns are read separately and zipped; a length
            # mismatch means a row parsed on one side and not the other,
            # so every pairing after it would be off by one.
            continue
        found = []
        for month_cell, day_cell in zip(months, days):
            hit = _fomc_row(month_cell, day_cell, year)
            if hit and _plausible(hit, today):
                found.append(Meeting(hit, "confirmed"))
        if len(found) >= MIN_MEETINGS_PER_YEAR:
            out.extend(found)
    return out or None


def _fomc_row(month_cell: str, day_cell: str, year: int) -> date | None:
    days = _FOMC_DAYS.match(day_cell.replace("&nbsp;", " ").strip())
    if not days:
        # "22 (notation vote)" and anything else unrecognised. A notation
        # vote is not a scheduled policy decision; dropping it is correct,
        # not a gap.
        return None
    first, second = days.group(1), days.group(2)
    parts = [p.strip().lower() for p in month_cell.split("/")]
    # A single-day meeting, or both days inside one month: first month wins.
    # A straddling meeting ends in the second month.
    month_name = parts[-1] if (second and len(parts) > 1) else parts[0]
    month = _MONTHS.get(month_name)
    if not month:
        return None
    if len(parts) > 1 and second:
        first_month = _MONTHS.get(parts[0])
        # Dec/Jan rolls the year as well as the month.
        if first_month and month < first_month:
            year += 1
    try:
        return date(year, month, int(second or first))
    except ValueError:
        return None


def fetch_fomc(today: date) -> list[Meeting] | None:
    return parse_fomc(_fetch(FOMC_URL) or "", today)


# ---------------------------------------------------------------------------
# Bank of England
# ---------------------------------------------------------------------------
# The page is a run of headings - "2026 confirmed dates", "2027 provisional
# dates" - each followed by a list of "Thursday 5 February" entries that
# carry no year of their own. Year and status both come from the heading
# above, so the page is walked in order rather than pattern-matched wholesale.
_BOE_HEADING = re.compile(r'(\d{4})\s+(confirmed|provisional)\s+dates',
                          re.I)
_BOE_DATE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday)\s+'
    r'(\d{1,2})\s+([A-Za-z]+)', re.I)


def parse_boe(html: str, today: date) -> list[Meeting] | None:
    if not html:
        return None
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html,
                  flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    sections = list(_BOE_HEADING.finditer(text))
    if not sections:
        return None

    out: list[Meeting] = []
    for i, head in enumerate(sections):
        year, status = int(head.group(1)), head.group(2).lower()
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        body = text[head.end():end]
        found = []
        for day_s, month_s in _BOE_DATE.findall(body):
            month = _MONTHS.get(month_s.lower())
            if not month:
                continue
            try:
                d = date(year, month, int(day_s))
            except ValueError:
                continue
            if _plausible(d, today):
                found.append(Meeting(d, status))
        # Dedupe within the section: the page names each date once in the
        # list and sometimes again in surrounding prose.
        found = [Meeting(d, status) for d in sorted({m.day for m in found})]
        if len(found) >= MIN_MEETINGS_PER_YEAR:
            out.extend(found)
    return out or None


def fetch_boe(today: date) -> list[Meeting] | None:
    return parse_boe(_fetch(BOE_URL) or "", today)


# ---------------------------------------------------------------------------
# Merge with the hardcoded floor
# ---------------------------------------------------------------------------
def reconcile(fetched: list[Meeting] | None, hardcoded: list[date],
              today: date, label: str) -> tuple[list[Meeting], list[str]]:
    """
    Combine a scrape with the hand-maintained table.

    The table is a floor, not a legacy. Where both cover the same month and
    disagree on the day, the hand-entered date wins and the disagreement is
    reported: a hand-checked date is worth more than a regex, and a silent
    overwrite is how a bad parse would reach the page. Beyond the table's
    horizon there is nothing to check against, and the scrape stands alone -
    which is the entire point of the exercise.
    """
    notes: list[str] = []
    floor = [Meeting(d, "confirmed") for d in hardcoded]

    if fetched is None:
        notes.append(f"{label}: published calendar could not be read; "
                     f"using the hand-maintained table only.")
        return sorted(floor, key=lambda m: m.day), notes

    by_month = {(m.day.year, m.day.month): m for m in fetched}
    kept: dict[date, Meeting] = {}

    for m in floor:
        scraped = by_month.get((m.day.year, m.day.month))
        if m.day >= today and scraped and scraped.day != m.day:
            notes.append(
                f"{label}: published calendar says "
                f"{scraped.day:%d %b %Y} where the table says "
                f"{m.day:%d %b %Y}. Keeping the table's date - check the "
                f"published calendar and correct calendar_data.py.")
        elif m.day >= today and not scraped:
            notes.append(
                f"{label}: published calendar has no meeting in "
                f"{m.day:%b %Y}, where the table has {m.day:%d %b %Y}. "
                f"Keeping the table's date.")
        kept[m.day] = m

    for m in fetched:
        # Never let a scrape displace a hand-entered date in a month the
        # table already speaks for; only extend past it.
        if (m.day.year, m.day.month) in {(k.year, k.month) for k in kept}:
            continue
        kept[m.day] = m

    return sorted(kept.values(), key=lambda m: m.day), notes


# ---------------------------------------------------------------------------
# Self-test - parsing logic against captured fragments (runs offline)
# ---------------------------------------------------------------------------
_FOMC_FIXTURE = """
<div class="panel panel-default"><div class="panel-heading"><h4>
<a id="1">2027 FOMC Meetings</a></h4></div>
<div class="fomc-meeting__month col-md-2"><strong>January</strong></div>
<div class="fomc-meeting__date col-lg-1">26-27</div>
<div class="fomc-meeting__month col-md-2"><strong>March</strong></div>
<div class="fomc-meeting__date col-lg-1">16-17*</div>
<div class="fomc-meeting__month col-md-2"><strong>Apr/May</strong></div>
<div class="fomc-meeting__date col-lg-1">30-1</div>
<div class="fomc-meeting__month col-md-2"><strong>June</strong></div>
<div class="fomc-meeting__date col-lg-1">8-9*</div>
<div class="fomc-meeting__month col-md-2"><strong>August</strong></div>
<div class="fomc-meeting__date col-lg-1">22 (notation vote)</div>
<div class="fomc-meeting__month col-md-2"><strong>September</strong></div>
<div class="fomc-meeting__date col-lg-1">14-15*</div>
<div class="fomc-meeting__month col-md-2"><strong>October</strong></div>
<div class="fomc-meeting__date col-lg-1">26-27</div>
<div class="fomc-meeting__month col-md-2"><strong>Dec/Jan</strong></div>
<div class="fomc-meeting__date col-lg-1">31-1</div>
<div class="fomc-meeting__month col-md-2"><strong>December</strong></div>
<div class="fomc-meeting__date col-lg-1">7-8*</div>
</div>
"""

_BOE_FIXTURE = """
<h2>2026 confirmed dates</h2>
<p>Thursday 5 February</p><p>February Monetary Policy Report</p>
<p>Thursday 19 March</p><p>March MPC Summary and minutes</p>
<p>Thursday 30 April</p><p>April Monetary Policy Report</p>
<p>Thursday 18 June</p><p>June MPC Summary and minutes</p>
<p>Thursday 30 July</p><p>July Monetary Policy Report</p>
<p>Thursday 17 September</p><p>September MPC Summary and minutes</p>
<p>Thursday 5 November</p><p>November Monetary Policy Report</p>
<p>Thursday&nbsp;17 December</p><p>December MPC Summary and minutes</p>
<h2>2027 provisional dates</h2>
<p>Thursday&nbsp;4 February</p><p>February Monetary Policy Report</p>
<p>Thursday 18 March&nbsp;</p><p>March MPC Summary and minutes</p>
<p>Thursday 29 April</p><p>April Monetary Policy Report</p>
<p>Thursday 17 June&nbsp;</p><p>June MPC Summary and minutes</p>
<p>Thursday 29 July&nbsp;</p><p>July Monetary Policy Report</p>
<p>Thursday 16 September&nbsp;</p><p>September MPC Summary and minutes</p>
<p>Thursday 4 November&nbsp;</p><p>November Monetary Policy Report</p>
<p>Thursday 16 December&nbsp;</p><p>December MPC Summary and minutes</p>
"""


def _selftest_offline() -> bool:
    import sys as _sys
    today = date(2026, 8, 27)

    # --- FOMC ------------------------------------------------------------
    got = parse_fomc(_FOMC_FIXTURE, today)
    assert got, "FOMC fixture parsed to nothing"
    days = [m.day for m in got]
    assert date(2027, 1, 27) in days, "two-day meeting: decision is day two"
    assert date(2027, 3, 17) in days, "SEP asterisk should not break parsing"
    assert date(2027, 5, 1) in days, f"Apr/May 30-1 should land 1 May: {days}"
    assert date(2028, 1, 1) in days, f"Dec/Jan 31-1 should roll year: {days}"
    assert date(2027, 8, 22) not in days, "notation vote is not a decision"
    assert all(m.confirmed for m in got)
    print(f"  FOMC parse       OK  {len(got)} meetings, "
          f"{min(days)} -> {max(days)}", file=_sys.stderr)

    # A panel that parses to too few meetings is a moved layout, not a
    # shortened year, and must be refused wholesale.
    thin = _FOMC_FIXTURE.replace("fomc-meeting__date", "renamed__date")
    assert parse_fomc(thin, today) is None, "thin parse should be refused"
    assert parse_fomc("", today) is None
    assert parse_fomc("<html>nothing here</html>", today) is None

    # Dates outside the horizon are a parse gone wrong.
    assert parse_fomc(_FOMC_FIXTURE, date(2050, 1, 1)) is None, \
        "implausible dates should be dropped"
    print("  FOMC guards      OK  (thin, empty, malformed, out-of-horizon)",
          file=_sys.stderr)

    # --- BoE -------------------------------------------------------------
    got = parse_boe(_BOE_FIXTURE, today)
    assert got, "BoE fixture parsed to nothing"
    by_day = {m.day: m for m in got}
    assert by_day[date(2026, 12, 17)].confirmed, "nbsp broke a confirmed date"
    assert by_day[date(2027, 2, 4)].status == "provisional", \
        "2027 dates are provisional and must not be relabelled"
    assert not any(m.confirmed for m in got if m.day.year == 2027)
    print(f"  BoE parse        OK  {len(got)} meetings, "
          f"{sum(m.confirmed for m in got)} confirmed", file=_sys.stderr)

    assert sum(m.day.year == 2026 for m in got) == 8, got
    assert sum(m.day.year == 2027 for m in got) == 8, got

    # A section that parses to too few dates is a moved layout, not a short
    # year, and is dropped rather than promoted into a complete one. Here
    # that leaves only the intact 2027 section standing.
    thin = re.sub(r"<p>Thursday (5 February|19 March|30 April|18 June|"
                  r"30 July)</p>", "", _BOE_FIXTURE)
    thinned = parse_boe(thin, today)
    assert thinned and all(m.day.year == 2027 for m in thinned), \
        f"a section below the meeting floor should be dropped: {thinned}"

    assert parse_boe("", today) is None
    assert parse_boe("<p>Thursday 5 February</p>", today) is None, \
        "dates with no heading have no year and must be refused"
    print("  BoE guards       OK  (thin section, empty, headingless)",
          file=_sys.stderr)

    # --- reconcile -------------------------------------------------------
    table = [date(2026, 9, 17), date(2026, 11, 5)]
    agree = [Meeting(date(2026, 9, 17), "confirmed"),
             Meeting(date(2026, 11, 5), "confirmed"),
             Meeting(date(2026, 12, 17), "confirmed")]
    merged, notes = reconcile(agree, table, today, "BoE MPC")
    assert [m.day for m in merged] == sorted(table + [date(2026, 12, 17)])
    assert not notes, f"agreement should be silent: {notes}"

    # A scrape disagreeing with a future hand-entered date loses, loudly.
    clash = [Meeting(date(2026, 9, 24), "confirmed")]
    merged, notes = reconcile(clash, table, today, "BoE MPC")
    assert date(2026, 9, 17) in [m.day for m in merged], "table must win"
    assert date(2026, 9, 24) not in [m.day for m in merged]
    assert len(notes) == 2, notes          # clash in Sep, silence in Nov
    assert "Keeping the table's date" in notes[0]

    # A failed fetch degrades to the table rather than to nothing.
    merged, notes = reconcile(None, table, today, "FOMC")
    assert [m.day for m in merged] == table
    assert notes and "could not be read" in notes[0]

    # A past disagreement is not worth reporting - the table's old dates are
    # history and the scrape has moved on.
    merged, notes = reconcile([Meeting(date(2020, 1, 2), "confirmed")],
                              [date(2020, 1, 9)], today, "FOMC")
    assert not notes, f"past dates should not warn: {notes}"
    print("  reconcile        OK  (agree, clash, no-fetch, past)",
          file=_sys.stderr)

    print("  cb_calendar self-test: OK", file=_sys.stderr)
    return True


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest_offline()
        raise SystemExit(0)

    today = date.today()
    print(f"Published calendar check for {today}\n")
    for name, fn in (("FOMC", fetch_fomc), ("BoE MPC", fetch_boe)):
        got = fn(today)
        if got is None:
            print(f"  {name}: FETCH OR PARSE FAILED")
            continue
        upcoming = [m for m in got if m.day >= today]
        print(f"  {name}: {len(got)} meetings parsed, "
              f"{len(upcoming)} still ahead")
        for m in upcoming:
            print(f"      {m.day:%a %d %b %Y}  ({m.status})")
