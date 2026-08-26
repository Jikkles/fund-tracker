"""
Forward calendar for the catalysts panel.

Two kinds of event, handled differently because their reliability differs:

  CENTRAL BANK DATES are published years in advance and essentially never
  move. They are hardcoded below, sourced from the Bank of England and
  Federal Reserve published calendars, and marked (confirmed). Extend the
  table when the next year is published - there is a staleness check that
  will warn you when it is running low.

  EARNINGS DATES are fetched live where possible. When a company has
  confirmed its date the fetch returns it and we mark (confirmed). When it
  has not, we fall back to a pattern estimate derived from the company's
  historical reporting rhythm and mark (estimated). Never the other way
  round: presenting an inferred date as confirmed is the specific failure
  this module exists to prevent.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, timedelta

from market_data import USER_AGENT, TIMEOUT

# ---------------------------------------------------------------------------
# Central bank calendar - published, confirmed, hardcoded.
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

# Roughly how far ahead we still have coverage. When the last hardcoded date
# is within this window, the run emits a warning to top the table up.
CALENDAR_LOW_WATER = timedelta(days=75)


def next_after(dates: list[date], today: date) -> date | None:
    upcoming = sorted(d for d in dates if d >= today)
    return upcoming[0] if upcoming else None


def calendar_health(today: date) -> list[str]:
    """Warn when the hardcoded tables are running out."""
    warnings = []
    for name, dates in (("BoE MPC", BOE_MPC_DATES), ("FOMC", FOMC_DATES)):
        remaining = [d for d in dates if d >= today]
        if not remaining:
            warnings.append(
                f"{name} calendar is EXHAUSTED - no future dates left. "
                f"Add the next year's published dates to calendar_data.py.")
        elif max(remaining) - today < CALENDAR_LOW_WATER:
            warnings.append(
                f"{name} calendar runs out on {max(remaining):%d %b %Y}. "
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

# Events with no fixed published date. Rather than fabricate one, state the
# expected window and say plainly that it is not scheduled. Update when a
# date is announced.
UNSCHEDULED_EVENTS: dict[str, str] = {
    "UK gilt supply / fiscal": (
        "Autumn Budget expected ~Nov 2026 (estimated - date not yet "
        "announced); DMO gilt remit updates ongoing"
    ),
    "TotalEnergies": (
        "Q3 2026 results ~late Oct 2026 (estimated); oil price exposure "
        "is continuous rather than event-driven"
    ),
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


def estimate_next(holding: str, today: date) -> str | None:
    """Pattern-based estimate. Always labelled (estimated)."""
    pattern = REPORTING_PATTERN.get(holding)
    if not pattern:
        return None
    months, position = pattern

    year, month = today.year, today.month
    for _ in range(14):
        if month in months and not (month == today.month and today.day > 20):
            # "(estimated)" says it all - an estimate is by definition not
            # a confirmed date, and the longer label crowded the panel.
            return f"~{position} {_MONTH_NAMES[month]} {year} (estimated)"
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


def next_event(holding: str, today: date) -> tuple[str, str] | None:
    """
    Return (date_string, source) for a holding's next event.
    date_string always states (confirmed) or (estimated).
    """
    if holding in ("UK rates (BoE)",):
        nxt = next_after(BOE_MPC_DATES, today)
        if nxt:
            return f"{fmt_date(nxt, with_weekday=True)} (confirmed)", "BoE published calendar"
        return None

    if holding in ("US rates (Fed)",):
        nxt = next_after(FOMC_DATES, today)
        if nxt:
            return f"{fmt_date(nxt, with_weekday=True)} (confirmed)", "Fed published calendar"
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
                    "Yahoo Finance calendar, earliest confirmed constituent")
        # Nothing confirmed: fall back to the earliest pattern estimate.
        estimates = [estimate_next(m, today) for m in members]
        estimates = [e for e in estimates if e]
        if estimates:
            return estimates[0], "historical reporting patterns (cluster)"
        return None

    # Fiscal/policy events with no fixed published date.
    if holding in UNSCHEDULED_EVENTS:
        return UNSCHEDULED_EVENTS[holding], "no fixed date published"

    ticker = HOLDING_TICKERS.get(holding)
    if ticker:
        confirmed = fetch_earnings_date(ticker)
        # Sanity-check: a "confirmed" date in the past, or absurdly far out,
        # is stale provider data, not a real confirmation.
        if confirmed and today <= confirmed <= today + timedelta(days=200):
            return (f"{fmt_date(confirmed, with_weekday=True)} (confirmed)",
                    f"Yahoo Finance calendar ({ticker})")

    est = estimate_next(holding, today)
    if est:
        return est, "historical reporting pattern"
    return None


def _season_end(dates: list[tuple[date, str]]) -> date:
    return max(d for d, _ in dates)


if __name__ == "__main__":
    today = date.today()
    print(f"Calendar check for {today}\n")
    for w in calendar_health(today):
        print(f"  WARNING: {w}")
    print(f"\n  Next BoE MPC: {next_after(BOE_MPC_DATES, today)}")
    print(f"  Next FOMC:    {next_after(FOMC_DATES, today)}\n")
    print("  Pattern estimates (no network needed):")
    for holding in list(REPORTING_PATTERN)[:8]:
        print(f"    {holding:24} {estimate_next(holding, today)}")
