"""TEMPORARY probe v6. The release JSON at /releases/<slug>/data is exactly
what the desk needs (release_date, finalised, cancelled, published). What is
missing is DISCOVERY: the calendar lists ten a page over 343 upcoming
releases and 429s if hammered, so paging to reach a monthly bulletin is out.
Find a filter that narrows the list in one request."""
import re, time, urllib.parse, urllib.request

UA = "fund-tracker/1.0 (+github actions; personal research desk)"
CAL = "https://www.ons.gov.uk/releasecalendar"
REL = re.compile(r'data-gtm-release-title\s*=\s*"([^"]*)".*?'
                 r'data-gtm-release-date="(\d{8})"', re.S)
TOTAL = re.compile(r'(\d+)\s+results', re.I)

SHAPES = [
    "?release-type=type-upcoming",
    "?release-type=type-upcoming&keywords=consumer+price+inflation",
    "?release-type=type-upcoming&query=consumer+price+inflation",
    "?release-type=type-upcoming&q=consumer+price+inflation",
    "?keywords=consumer+price+inflation",
    "?release-type=type-upcoming&page=2",
    "?release-type=type-upcoming&fromDateDay=14&fromDateMonth=9&fromDateYear=2026"
    "&toDateDay=20&toDateMonth=9&toDateYear=2026",
]

for shape in SHAPES:
    url = CAL + shape
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:                            # noqa: BLE001 - probe
        print(f"-- FAILED {type(e).__name__}: {e}\n   {shape}")
        time.sleep(2); continue
    hits = REL.findall(body)
    tot = TOTAL.search(body)
    print(f"-- total={tot.group(1) if tot else '?':>4}  n={len(hits):>2}  {shape}")
    for t, d in hits[:4]:
        print(f"      {d}  {t[:66]}")
    time.sleep(2)
