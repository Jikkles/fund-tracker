"""
Forward calendar for the catalysts panel.

Two kinds of event, handled differently because their reliability differs:

  CENTRAL BANK DATES are read from the published calendars by cb_calendar,
  which scrapes the Bank of England and Federal Reserve pages. The tables
  below are no longer the source - they are a floor. Where a scrape and a
  hand-entered future date disagree, the hand-entered one wins and the run
  says so; beyond the table's horizon the scrape stands alone, which is the
  point. A page that cannot be read degrades to the table rather than to
  nothing, so the desk keeps working when the Fed's markup changes.

  The Bank publishes one year as confirmed and the next as provisional, and
  that distinction is carried through to the label. A provisional date is
  not a confirmed one.

  EARNINGS DATES are fetched live where possible. When a company has
  confirmed its date the fetch returns it and we mark (confirmed). When it
  has not, we fall back to a pattern estimate derived from the company's
  historical reporting rhythm and mark (estimated). Never the other way
  round: presenting an inferred date as confirmed is the specific failure
  this module exists to prevent.

Every resolved event also carries an ISO **sort anchor**. The displayed string
is deliberately fuzzy where the date is - "~mid Oct 2026" is the honest form of
a pattern estimate - but a fuzzy string cannot be ordered or aged out, which is
why the catalyst panel used to publish events in arbitrary order with last
month's still in it. The anchor exists purely to sort and to filter; it is
never displayed, and for an estimate it is the middle of the stated third of
the month (early=5th, mid=15th, late=25th), not a claim of precision.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, timedelta

import cb_calendar as cb
from market_data import USER_AGENT, TIMEOUT

# ---------------------------------------------------------------------------
# Central bank calendar - a floor under the published calendars, not the
# source. cb_calendar reads the real pages; these dates are what the desk
# falls back to when it cannot, and what a suspect scrape is checked against.
# Source: bankofengland.co.uk/monetary-policy/upcoming-mpc-dates
#         federalreserve.gov/monetarypolicy/fomccalendars.htm
# ---------------------------------------------------------------------------
BOE_MPC_DATES: list[date] = [
    date(2026, 9, 17),
    date(2026, 11, 5),    # + Monetary Policy Report
    date(2026, 12, 17),
]

FOMC_DATES: list[date] = [
    date(2026, 10, 28),   # 27-28 Oct, decision on the 28th
]

# holding name -> (report label, fallback table, fetcher, source credit)
CENTRAL_BANKS: dict[str, tuple[str, list[date], object, str]] = {
    "UK rates (BoE)": ("BoE MPC", BOE_MPC_DATES, cb.fetch_boe,
                       "BoE published calendar"),
    "US rates (Fed)": ("FOMC", FOMC_DATES, cb.fetch_fomc,
                       "Fed published calendar"),
}

# Roughly how far ahead we still have coverage. When the last known date is
# within this window, the run emits a warning.
CALENDAR_LOW_WATER = timedelta(days=75)

# One fetch per page per run. next_event is called per holding and
# calendar_health runs afterwards; without this the pages would be pulled
# several times for the same answer.
_CALENDAR_CACHE: dict[str, tuple[list[cb.Meeting], list[str]]] = {}


def load_calendar(holding: str, today: date
                  ) -> tuple[list[cb.Meeting], list[str]]:
    """Published calendar reconciled against the fallback table, cached."""
    if holding not in _CALENDAR_CACHE:
        label, table, fetch, _ = CENTRAL_BANKS[holding]
        _CALENDAR_CACHE[holding] = cb.reconcile(
            fetch(today), table, today, label)
    return _CALENDAR_CACHE[holding]


def next_after(dates: list[date], today: date) -> date | None:
    upcoming = sorted(d for d in dates if d >= today)
    return upcoming[0] if upcoming else None


def next_meeting(meetings: list[cb.Meeting], today: date) -> cb.Meeting | None:
    upcoming = sorted((m for m in meetings if m.day >= today),
                      key=lambda m: m.day)
    return upcoming[0] if upcoming else None


def calendar_health(today: date) -> list[str]:
    """
    Warn when a calendar is running out, and surface any disagreement
    between the published page and the fallback table.
    """
    warnings = []
    for holding, (label, _table, _fetch, _src) in CENTRAL_BANKS.items():
        meetings, notes = load_calendar(holding, today)
        warnings.extend(notes)
        remaining = [m.day for m in meetings if m.day >= today]
        if not remaining:
            warnings.append(
                f"{label} calendar is EXHAUSTED - no future dates left, and "
                f"the published page did not supply any. Add the next "
                f"year's dates to calendar_data.py.")
        elif max(remaining) - today < CALENDAR_LOW_WATER:
            warnings.append(
                f"{label} calendar runs out on {max(remaining):%d %b %Y}. "
                f"Top it up from the published calendar.")
    return warnings


# ---------------------------------------------------------------------------
# Earnings dates
# ---------------------------------------------------------------------------
# Holding name (as it appears in funds.json) -> Yahoo ticker.
HOLDING_TICKERS: dict[str, str] = {
    "NVIDIA": "NVDA",
    "TSMC": "TSM",
    "Microsoft": "MSFT",
    "Apple": "AAPL",
    "Amazon": "AMZN",
    "Alphabet": "GOOGL",
    "Meta Platforms": "META",
    "ASML": "ASML",
    "Samsung Electronics": "005930.KS",
    "SK Hynix": "000660.KS",
    "Tencent": "0700.HK",
    "Novo Nordisk": "NVO",
    "TotalEnergies": "TTE",
    "HDFC Bank": "HDB",
    "SoftBank Group": "9984.T",
    "Advantest": "6857.T",
    "Sumitomo Mitsui Trust Group": "8309.T",
}

# Fallback reporting rhythm when no confirmed date is available: which months
# the company reports in, and roughly where in the month.
REPORTING_PATTERN: dict[str, tuple[list[int], str]] = {
    "NVIDIA":                      ([2, 5, 8, 11], "late"),
    "TSMC":                        ([1, 4, 7, 10], "mid"),
    "ASML":                        ([1, 4, 7, 10], "mid"),
    "Microsoft":                   ([1, 4, 7, 10], "late"),
    "Apple":                       ([2, 5, 8, 11], "early"),
    "Amazon":                      ([2, 4, 8, 10], "late"),
    "Alphabet":                    ([2, 4, 7, 10], "late"),
    "Meta Platforms":              ([1, 4, 7, 10], "late"),
    "Samsung Electronics":         ([1, 4, 7, 10], "late"),
    "SK Hynix":                    ([1, 4, 7, 10], "late"),
    "Tencent":                     ([3, 5, 8, 11], "mid"),
    "Novo Nordisk":                ([2, 5, 8, 11], "early"),
    "TotalEnergies":               ([2, 4, 7, 10], "late"),
    "HDFC Bank":                   ([1, 4, 7, 10], "mid"),
    "SoftBank Group":              ([2, 5, 8, 11], "mid"),
    "Advantest":                   ([1, 4, 7, 10], "late"),
    "Sumitomo Mitsui Trust Group": ([2, 5, 8, 11], "mid"),
}

_MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_date(d: date, with_weekday: bool = False) -> str:
    """
    Cross-platform 'day-without-leading-zero' formatting.

    strftime's no-leading-zero flag differs by OS: Linux/Mac use %-d, Windows
    uses %#d, and neither works on the other. Rather than branch on os.name
    (which breaks the moment this runs somewhere unexpected - a container
    image, a different runner OS after a workflow change), we build the
    string from plain integers, which is entirely platform-independent.
    """
    s = f"{d.day} {_MONTH_NAMES[d.month]} {d.year}"
    if with_weekday:
        s = f"{_WEEKDAY_NAMES[d.weekday()]} {s}"
    return s

# Aggregate catalyst entries that stand for a whole reporting season rather
# than one company. Resolved to the earliest upcoming constituent date.
CLUSTER_MEMBERS: dict[str, list[str]] = {
    "Full mega-cap cluster": [
        "TSMC", "ASML", "Microsoft", "Alphabet", "Meta Platforms",
        "Apple", "Amazon", "NVIDIA",
    ],
    "NVIDIA + mega-cap cluster": [
        "NVIDIA", "TSMC", "Microsoft", "Alphabet", "Meta Platforms",
        "Apple", "Amazon",
    ],
}

# Events with no fixed published date and no company calendar to fetch. These
# still roll: the window is generated from an annual rhythm the same way an
# earnings estimate is, rather than being written out with a year in it. The
# previous hardcoded strings named 2026 and would have gone on naming 2026
# through the whole of 2027 - a stale date presented as a forward-looking one,
# which is the exact failure this module exists to prevent, arrived at from
# the other direction.
#   holding -> (months it lands in, where in the month, what it is)
ANNUAL_POLICY_EVENTS: dict[str, tuple[list[int], str, str]] = {
    "UK gilt supply / fiscal": (
        [11], "mid",
        "Autumn Budget, date not yet announced; DMO gilt remit updates ongoing",
    ),
}

# Context appended to a holding's resolved date where the date alone would
# overstate how event-driven the fund's exposure actually is. The date itself
# still comes from the normal confirmed-then-estimated ladder, so it rolls.
EVENT_NOTES: dict[str, str] = {
    "TotalEnergies": ("oil price exposure is continuous rather than "
                      "event-driven"),
}


def fetch_earnings_date(ticker: str) -> date | None:
    """Confirmed next earnings date from Yahoo, or None."""
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
           f"{ticker}?modules=calendarEvents")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError):
        return None

    try:
        events = (payload["quoteSummary"]["result"][0]
                  ["calendarEvents"]["earnings"]["earningsDate"])
        if not events:
            return None
        return date.fromtimestamp(events[0]["raw"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# Where in the month a "early"/"mid"/"late" window is anchored for sorting.
# Ordering needs a single day; the published string keeps the honest window.
_ANCHOR_DAY = {"early": 5, "mid": 15, "late": 25}


def pattern_window(months: list[int], position: str,
                   today: date) -> tuple[str, date] | None:
    """Next (label, sort anchor) for a company or event reporting rhythm."""
    year, month = today.year, today.month
    for _ in range(14):
        if month in months and not (month == today.month and today.day > 20):
            # "(estimated)" says it all - an estimate is by definition not
            # a confirmed date, and the longer label crowded the panel.
            label = f"~{position} {_MONTH_NAMES[month]} {year} (estimated)"
            return label, date(year, month, _ANCHOR_DAY.get(position, 15))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def estimate_next(holding: str, today: date) -> tuple[str, date] | None:
    """Pattern-based estimate. Always labelled (estimated)."""
    pattern = REPORTING_PATTERN.get(holding)
    if not pattern:
        return None
    return pattern_window(pattern[0], pattern[1], today)


def next_event(holding: str, today: date) -> tuple[str, str, str | None] | None:
    """
    Return (date_string, source, sort_anchor) for a holding's next event.

    date_string always states (confirmed) or (estimated). sort_anchor is an
    ISO date used only to order and to age out the catalyst panel, or None
    where the event genuinely resolves to no date at all.
    """
    if holding in CENTRAL_BANKS:
        _label, _table, _fetch, source = CENTRAL_BANKS[holding]
        meetings, _notes = load_calendar(holding, today)
        nxt = next_meeting(meetings, today)
        if nxt:
            # (provisional) is the Bank's own word for its next-year dates.
            # Relabelling one as confirmed would be the same failure as
            # dressing a pattern estimate up as a fetched date.
            return (f"{fmt_date(nxt.day, with_weekday=True)} ({nxt.status})",
                    source, nxt.day.isoformat())
        return None

    # Aggregate "cluster" entries track a reporting season, not one company.
    # Anchor them to the earliest upcoming date among their constituents so
    # the panel shows when the season actually starts.
    if holding in CLUSTER_MEMBERS:
        members = CLUSTER_MEMBERS[holding]
        dates: list[tuple[date, str]] = []
        for member in members:
            ticker = HOLDING_TICKERS.get(member)
            if not ticker:
                continue
            confirmed = fetch_earnings_date(ticker)
            if confirmed and today <= confirmed <= today + timedelta(days=200):
                dates.append((confirmed, member))
        if dates:
            dates.sort()
            first_date, first_member = dates[0]
            return (f"{first_member} {fmt_date(first_date)} (confirmed), "
                    f"then the wider cluster through "
                    f"{_season_end(dates):%b %Y}",
                    "Yahoo Finance calendar, earliest confirmed constituent",
                    first_date.isoformat())
        # Nothing confirmed: fall back to the earliest pattern estimate. Sort
        # by the anchor, not by list order - the members are listed by weight.
        estimates = [e for e in (estimate_next(m, today) for m in members) if e]
        if estimates:
            label, anchor = min(estimates, key=lambda e: e[1])
            return (label, "historical reporting patterns (cluster)",
                    anchor.isoformat())
        return None

    # Fiscal/policy events with no company calendar to fetch.
    if holding in ANNUAL_POLICY_EVENTS:
        months, position, note = ANNUAL_POLICY_EVENTS[holding]
        hit = pattern_window(months, position, today)
        if hit:
            label, anchor = hit
            return (f"{label} - {note}", "no fixed date published",
                    anchor.isoformat())
        return None

    ticker = HOLDING_TICKERS.get(holding)
    if ticker:
        confirmed = fetch_earnings_date(ticker)
        # Sanity-check: a "confirmed" date in the past, or absurdly far out,
        # is stale provider data, not a real confirmation.
        if confirmed and today <= confirmed <= today + timedelta(days=200):
            return (_with_note(holding,
                               f"{fmt_date(confirmed, with_weekday=True)} "
                               f"(confirmed)"),
                    f"Yahoo Finance calendar ({ticker})",
                    confirmed.isoformat())

    est = estimate_next(holding, today)
    if est:
        label, anchor = est
        return (_with_note(holding, label), "historical reporting pattern",
                anchor.isoformat())
    return None


def _with_note(holding: str, label: str) -> str:
    note = EVENT_NOTES.get(holding)
    return f"{label}; {note}" if note else label


def _season_end(dates: list[tuple[date, str]]) -> date:
    return max(d for d, _ in dates)


def _selftest_offline() -> bool:
    """
    Exercise the date logic with no network. Pattern windows are pure
    arithmetic; the central bank cache is seeded by hand so the fetch never
    runs - reading the published pages is cb_calendar's business and has its
    own self-test.
    """
    import sys as _sys

    # Pattern windows roll forward, and the anchor lands inside the month
    # the label names.
    label, anchor = pattern_window([10], "mid", date(2026, 8, 27))
    assert label == "~mid Oct 2026 (estimated)", label
    assert anchor == date(2026, 10, 15), anchor

    # Past the 20th of a reporting month, the window moves to the next one
    # rather than pointing at a date that has all but arrived.
    label, anchor = pattern_window([8, 11], "late", date(2026, 8, 27))
    assert label == "~late Nov 2026 (estimated)", label
    assert anchor == date(2026, 11, 25), anchor

    # ...and before the 20th it stays in the current month.
    label, anchor = pattern_window([8], "late", date(2026, 8, 3))
    assert anchor == date(2026, 8, 25), anchor

    # Rolling over a year boundary.
    label, anchor = pattern_window([1], "early", date(2026, 11, 30))
    assert label == "~early Jan 2027 (estimated)", label

    # A rhythm that never matches must fail rather than loop.
    assert pattern_window([], "mid", date(2026, 8, 27)) is None

    # Central bank events resolve to the published date, with an anchor
    # equal to the date itself. Seed the cache so this stays offline - the
    # fetch itself is cb_calendar's to test, not ours.
    today = date(2026, 8, 27)
    _CALENDAR_CACHE["UK rates (BoE)"] = ([
        cb.Meeting(date(2026, 9, 17), "confirmed"),
        cb.Meeting(date(2027, 2, 4), "provisional"),
    ], [])
    hit = next_event("UK rates (BoE)", today)
    assert hit == ("Thu 17 Sep 2026 (confirmed)", "BoE published calendar",
                   "2026-09-17"), hit

    # Past that date the next one up is provisional, and must say so rather
    # than inherit the confirmed label from the entry before it.
    hit = next_event("UK rates (BoE)", date(2026, 9, 18))
    assert hit and hit[0].endswith("(provisional)"), hit
    assert hit[2] == "2027-02-04", hit

    # An exhausted calendar resolves to nothing rather than to a past date.
    _CALENDAR_CACHE["US rates (Fed)"] = (
        [cb.Meeting(date(2026, 10, 28), "confirmed")], [])
    assert next_event("US rates (Fed)", date(2026, 10, 29)) is None
    assert any("EXHAUSTED" in w for w in calendar_health(date(2026, 10, 29)))
    _CALENDAR_CACHE.clear()

    # An annual policy event rolls with the calendar instead of naming a
    # year that will go stale.
    hit = next_event("UK gilt supply / fiscal", date(2027, 3, 1))
    assert hit and "Nov 2027" in hit[0], hit
    assert hit[2] == "2027-11-15", hit

    # An event note is appended to the rolling date, not substituted for it.
    # Tested through estimate_next rather than next_event: a holding with a
    # ticker would reach for the network, and this test must run offline.
    label, anchor = estimate_next("TotalEnergies", date(2026, 12, 1))
    assert label == "~late Feb 2027 (estimated)", label
    noted = _with_note("TotalEnergies", label)
    assert noted.startswith("~late Feb 2027 (estimated); "), noted
    assert "continuous rather than event-driven" in noted, noted
    assert _with_note("NVIDIA", label) == label, "unrelated holding gained a note"

    # Every anchor a holding can produce must parse as an ISO date and must
    # not already be in the past - the panel filters on exactly that.
    today = date(2026, 8, 27)
    for holding in REPORTING_PATTERN:
        hit = estimate_next(holding, today)
        assert hit, f"{holding} resolved to nothing"
        assert hit[1] >= today, (holding, hit)
    for holding in ANNUAL_POLICY_EVENTS:
        hit = next_event(holding, today)
        assert hit, f"{holding} resolved to nothing"
        assert date.fromisoformat(hit[2]) >= today, (holding, hit)

    # A holding nothing knows about resolves to nothing rather than a guess.
    assert estimate_next("Some Company Nobody Tracks", today) is None

    print("  calendar self-test: OK", file=_sys.stderr)
    return True


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest_offline()
        raise SystemExit(0)

    today = date.today()
    print(f"Calendar check for {today}\n")
    for w in calendar_health(today):
        print(f"  WARNING: {w}")

    print()
    for holding, (label, _t, _f, _s) in CENTRAL_BANKS.items():
        meetings, _ = load_calendar(holding, today)
        nxt = next_meeting(meetings, today)
        ahead = sum(1 for m in meetings if m.day >= today)
        print(f"  Next {label:8} {fmt_date(nxt.day, True) if nxt else '-':18}"
              f"({nxt.status if nxt else 'none'}), {ahead} ahead")

    print("\n  Pattern estimates (no network needed):")
    for holding in list(REPORTING_PATTERN)[:8]:
        est = estimate_next(holding, today)
        print(f"    {holding:24} {est[0] if est else '-':32} "
              f"sorts as {est[1] if est else '-'}")
