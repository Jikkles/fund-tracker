"""
Scheduled UK statistical releases, read from the ONS release calendar.

The catalyst panel knew about two kinds of macro event - a BoE decision and
an FOMC decision - and nothing else. The prints those committees actually
react to were missing: the desk could tell you when the MPC next meets but
not when the inflation number it will be looking at lands. This module adds
the releases themselves.

  DISCOVERY   /releasecalendar?release-type=type-upcoming&keywords=<terms>
              returns the upcoming calendar filtered to one statistic,
              soonest first, ten to a page. One request per statistic; the
              unfiltered calendar runs to several hundred entries and
              paging it to reach a monthly bulletin is dozens of requests
              for one date.

  CONFIRMATION  /releases/<slug>/data returns that release's own record as
              JSON - release_date, finalised, cancelled, published. This is
              the authoritative version of what the listing shows, and it
              is where the confirmed/provisional distinction comes from.

ONS PUBLISHES A PROVISIONAL/CONFIRMED DISTINCTION AND IT IS CARRIED THROUGH.
`finalised` is the page's own word for it, exactly as the Bank's "provisional
dates" heading is in cb_calendar. A provisional date presented as confirmed
is the same failure in a different costume.

TITLE MATCHING IS THE LOAD-BEARING GUARD. A keyword search is a search, not a
lookup: "consumer price inflation" also returns "Producer price inflation"
and half a dozen index time-series. Every statistic therefore declares the
prefix its release title must start with, and anything that does not match is
dropped rather than taken as near enough. Publishing PPI's date under a CPI
label would be a confident wrong number, which is the one thing the desk must
never do.

ONS RATE-LIMITS. Four requests in quick succession returned 429, so calls are
spaced and a 429 is retried with a longer wait. The daily run is already a
heavy fetcher; this module must not be what gets it blocked.

NOTE ON TESTING: like market_data, the sandbox this was written in cannot
reach ons.gov.uk. The fixtures below are real - captured from the live
responses on a GitHub Actions runner - so the parsing is tested against what
ONS actually served rather than against an invented shape.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date

from market_data import USER_AGENT, TIMEOUT

BASE = "https://www.ons.gov.uk"
CALENDAR = BASE + "/releasecalendar"

# ONS answered 429 to four requests fired back to back. One second between
# calls and a longer wait on a refusal keeps the daily run welcome.
PAUSE_SECONDS = 1.5
RETRY_PAUSE_SECONDS = 8.0

# A release date outside this window is a parse that has gone wrong, not a
# statistic scheduled four years out.
HORIZON_DAYS_BACK = 30
HORIZON_DAYS_FORWARD = 400


@dataclass(frozen=True)
class Release:
    """One scheduled statistical release."""
    day: date
    title: str
    uri: str
    status: str            # "confirmed" | "provisional"

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


# ---------------------------------------------------------------------------
# The statistics the desk tracks.
#
# keywords    what to search the calendar for.
# prefix      what the release title must begin with, lowercased. This is the
#             guard: the search is fuzzy, this is not.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Statistic:
    label: str
    keywords: str
    prefix: str


STATISTICS: dict[str, Statistic] = {
    "UK inflation (ONS)": Statistic(
        "UK CPI", "consumer price inflation", "consumer price inflation"),
    "UK labour market (ONS)": Statistic(
        "UK labour market", "labour market overview", "labour market overview"),
    "UK GDP (ONS)": Statistic(
        "UK monthly GDP", "gdp monthly estimate", "gdp monthly estimate"),
}


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _fetch(url: str) -> str | None:
    """GET a page, retrying once on a rate-limit refusal."""
    for attempt in (0, 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # 429 is ONS asking us to slow down, which is worth honouring
            # once. Any other status is an answer, not a delay.
            if e.code == 429 and attempt == 0:
                time.sleep(RETRY_PAUSE_SECONDS)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Parsing the calendar listing
# ---------------------------------------------------------------------------
# Each hit is an anchor carrying the release's url, title and date as data
# attributes. Reading those rather than the rendered text avoids depending on
# the surrounding layout, which is the part most likely to be restyled. Note
# the space either side of "=" on the title attribute: that is ONS's markup,
# not a typo.
_ITEM = re.compile(
    r'data-gtm-release-title\s*=\s*"(?P<title>[^"]*)"'
    r'.*?data-gtm-release-url\s*=\s*"(?P<uri>[^"]*)"'
    r'.*?data-gtm-release-date\s*=\s*"(?P<date>\d{8})"',
    re.S)


def _clean_title(raw: str) -> str:
    """
    Strip the search highlighting ONS wraps around matched terms.

    A keyword hit comes back as "&lt;em class=...&gt;Consumer&lt;/em&gt;
    &lt;em...&gt;price&lt;/em&gt; inflation, UK: August 2026" - the markup is
    escaped inside the attribute, so it survives one unescape and has to be
    stripped as tags afterwards.
    """
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_listing(page: str, today: date) -> list[tuple[date, str, str]] | None:
    """
    (day, title, uri) for each release on a calendar page, in page order.

    None means the page could not be understood at all, which is distinct
    from a page that was read and held no releases.
    """
    if not page:
        return None
    out: list[tuple[date, str, str]] = []
    for m in _ITEM.finditer(page):
        stamp = m.group("date")
        try:
            day = date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
        except ValueError:
            continue
        if not _plausible(day, today):
            continue
        out.append((day, _clean_title(m.group("title")), m.group("uri")))
    return out


def _plausible(day: date, today: date) -> bool:
    return (-HORIZON_DAYS_BACK
            <= (day - today).days
            <= HORIZON_DAYS_FORWARD)


# ---------------------------------------------------------------------------
# Parsing one release's own record
# ---------------------------------------------------------------------------
def parse_release(payload: str) -> tuple[date, str, bool] | None:
    """
    (release day, status, cancelled) from a release page's JSON.

    `finalised` is ONS's own confirmed/provisional flag. A release with no
    usable date returns None rather than a guess.
    """
    try:
        doc = json.loads(payload)
        desc = doc["description"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    stamp = desc.get("release_date") or ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(stamp))
    if not m:
        return None
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    status = "confirmed" if desc.get("finalised") else "provisional"
    return day, status, bool(desc.get("cancelled"))


# ---------------------------------------------------------------------------
# Putting the two together
# ---------------------------------------------------------------------------
def next_release(holding: str, today: date) -> Release | None:
    """
    The next scheduled release for a tracked statistic, or None.

    None covers every failure - page unreadable, nothing upcoming, nothing
    whose title matches the statistic - because for the caller they are the
    same answer: this catalyst has no date today. A wrong date would not be.
    """
    stat = STATISTICS.get(holding)
    if not stat:
        return None

    url = (f"{CALENDAR}?release-type=type-upcoming&keywords="
           f"{urllib.parse.quote_plus(stat.keywords)}")
    listing = parse_listing(_fetch(url) or "", today)
    if not listing:
        return None

    for day, title, uri in sorted(listing, key=lambda r: r[0]):
        if day < today:
            continue
        # The guard. A search for "consumer price inflation" returns producer
        # price inflation and a shelf of index time-series; only a title that
        # actually starts with this statistic's name is this statistic.
        if not title.lower().startswith(stat.prefix):
            continue

        time.sleep(PAUSE_SECONDS)
        detail = parse_release(_fetch(f"{BASE}{uri}/data") or "")
        if not detail:
            # The listing had a date but the record could not be read, so the
            # confirmed/provisional flag is unknown. Publishing it as either
            # would be inventing the part we could not read.
            continue
        release_day, status, cancelled = detail
        if cancelled or release_day < today:
            continue
        return Release(release_day, title, uri, status)
    return None


# ---------------------------------------------------------------------------
# Self-test - parsing logic against real captured responses (runs offline)
# ---------------------------------------------------------------------------
# Captured from https://www.ons.gov.uk/releasecalendar?release-type=type-
# upcoming on a runner. Trimmed to two items; attributes and their spacing
# are as served, including the space either side of "=" on the title.
_LISTING_FIXTURE = """
    <li class="ons-list__item ons-u-mt-l">
      <a
        href="/releases/businessinsightsandimpactontheukeconomy3september2026"
        class="ons-u-fs-m ons-u-td-no ons-u-d-b"
        data-gtm-release-title = "Business insights and impact on the UK economy: 3 September 2026"
        data-gtm-release-url="/releases/businessinsightsandimpactontheukeconomy3september2026"
        data-gtm-release-date="20260903"
        data-gtm-release-time="09:30"
      >Business insights and impact on the UK economy: 3 September 2026</a>
      <div class="ons-u-mt-xs">
        <span class="ons-u-fs-r--b">Release date:</span>
        <span>3 September 2026 9:30am</span><span>|</span><span>Published</span>
      </div>
    </li>
    <li class="ons-list__item ons-u-mt-l">
      <a
        href="/releases/consumerpriceinflationukaugust2026"
        class="ons-u-fs-m ons-u-td-no ons-u-d-b"
        data-gtm-release-title = "&lt;em class=&#34;ons-highlight&#34;&gt;Consumer&lt;/em&gt; &lt;em class=&#34;ons-highlight&#34;&gt;price&lt;/em&gt; &lt;em class=&#34;ons-highlight&#34;&gt;inflation&lt;/em&gt;, UK: August 2026"
        data-gtm-release-url="/releases/consumerpriceinflationukaugust2026"
        data-gtm-release-date="20260916"
        data-gtm-release-time="07:00"
      >Consumer price inflation, UK: August 2026</a>
      <div class="ons-u-mt-xs">
        <span class="ons-u-fs-r--b">Release date:</span>
        <span>16 September 2026 7:00am</span><span>|</span><span>Confirmed</span>
      </div>
    </li>
"""

# Captured verbatim from
# https://www.ons.gov.uk/releases/consumerpriceinflationukaugust2026/data
_RELEASE_FIXTURE = json.dumps({
    "date_changes": [],
    "description": {
        "cancellation_notice": [],
        "cancelled": False,
        "contact": {"email": "", "name": "", "telephone": ""},
        "finalised": True,
        "migration_link": "",
        "national_statistic": True,
        "next_release": "",
        "provisional_date": "",
        "published": False,
        "release_date": "2026-09-16T06:00:00.000Z",
        "summary": "Price indices percentage changes and weights for the "
                   "different measures of consumer price inflation",
        "survey": "",
        "title": "Consumer price inflation, UK: August 2026",
        "welsh_statistic": False,
    },
    "links": [],
    "markdown": [],
    "related_api_datasets": [],
    "related_datasets": [],
    "related_documents": [],
    "related_methodology": [],
    "related_methodology_article": [],
    "uri": "/releases/consumerpriceinflationukaugust2026",
})


def _selftest_offline() -> bool:
    import sys as _sys
    today = date(2026, 9, 4)

    # --- listing ---------------------------------------------------------
    rows = parse_listing(_LISTING_FIXTURE, today)
    assert rows and len(rows) == 2, rows
    by_title = {t: (d, u) for d, t, u in rows}
    assert "Consumer price inflation, UK: August 2026" in by_title, rows
    day, uri = by_title["Consumer price inflation, UK: August 2026"]
    assert day == date(2026, 9, 16), day
    assert uri == "/releases/consumerpriceinflationukaugust2026", uri
    print(f"  listing parse    OK  {len(rows)} releases, highlight stripped",
          file=_sys.stderr)

    # Highlighting must not leak into the title, or the prefix guard below
    # would never match and every keyword hit would be discarded.
    assert not any("<em" in t or "ons-highlight" in t for _, t, _ in rows), rows

    assert parse_listing("", today) is None
    assert parse_listing("<html>nothing</html>", today) == []
    # A date outside the horizon is a parse gone wrong, not a release.
    assert parse_listing(_LISTING_FIXTURE, date(2035, 1, 1)) == []
    print("  listing guards   OK  (empty, markup-free, out-of-horizon)",
          file=_sys.stderr)

    # --- release record --------------------------------------------------
    got = parse_release(_RELEASE_FIXTURE)
    assert got == (date(2026, 9, 16), "confirmed", False), got

    # finalised false is ONS's own hedge and must not read as confirmed.
    prov = json.loads(_RELEASE_FIXTURE)
    prov["description"]["finalised"] = False
    assert parse_release(json.dumps(prov))[1] == "provisional"

    cancelled = json.loads(_RELEASE_FIXTURE)
    cancelled["description"]["cancelled"] = True
    assert parse_release(json.dumps(cancelled))[2] is True

    undated = json.loads(_RELEASE_FIXTURE)
    undated["description"]["release_date"] = ""
    assert parse_release(json.dumps(undated)) is None
    assert parse_release("") is None
    assert parse_release("{}") is None
    assert parse_release('{"description":{}}') is None
    print("  release parse    OK  (confirmed, provisional, cancelled, "
          "undated, malformed)", file=_sys.stderr)

    # --- the title guard -------------------------------------------------
    # The failure this exists to stop: a keyword search for consumer price
    # inflation also returns producer price inflation, and taking the first
    # hit would publish PPI's date under a CPI label.
    ppi = _LISTING_FIXTURE.replace(
        "&lt;em class=&#34;ons-highlight&#34;&gt;Consumer&lt;/em&gt;",
        "Producer")
    rows = parse_listing(ppi, today)
    titles = [t for _, t, _ in rows]
    stat = STATISTICS["UK inflation (ONS)"]
    matched = [t for t in titles if t.lower().startswith(stat.prefix)]
    assert not matched, f"producer price inflation must not match CPI: {titles}"
    print("  title guard      OK  (producer price inflation rejected)",
          file=_sys.stderr)

    print("  stat_calendar self-test: OK", file=_sys.stderr)
    return True


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest_offline()
    else:
        for name in STATISTICS:
            print(f"{name}: {next_release(name, date.today())}")
