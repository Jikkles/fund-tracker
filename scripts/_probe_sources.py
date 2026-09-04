"""TEMPORARY probe v3. BLS is a hard 403 from runner IPs on any UA, so test
whether any other keyless US release calendar is reachable, and work out how
to narrow the ONS list to the releases the desk actually cares about."""
import re, urllib.request

UA = "fund-tracker/1.0 (+github actions; personal research desk)"
REL = re.compile(
    r'data-gtm-release-title\s*=\s*"([^"]*)".*?'
    r'data-gtm-release-date="(\d{8})"', re.S)

US = [
    "https://www.bea.gov/news/schedule",
    "https://www.census.gov/economic-indicators/calendar.html",
    "https://www.federalreserve.gov/newsevents/calendar.htm",
    "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0",
]
print("########## keyless US sources")
for u in US:
    try:
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            b = r.read().decode("utf-8", "replace")
        print(f"  OK  {r.status} bytes={len(b):>7}  {u}")
    except Exception as e:                            # noqa: BLE001 - probe
        print(f"  --  {type(e).__name__}: {e}  {u}")

print()
print("########## ONS: does the upcoming list carry the big releases?")
u = "https://www.ons.gov.uk/releasecalendar?release-type=type-upcoming&size=100"
req = urllib.request.Request(u, headers={"User-Agent": UA})
with urllib.request.urlopen(req, timeout=30) as r:
    body = r.read().decode("utf-8", "replace")
hits = REL.findall(body)
print(f"  {u}\n  releases={len(hits)}")
WANT = ("consumer price", "inflation", "labour market", "gross domestic",
        "gdp", "retail sales", "public sector finances")
for t, d in hits:
    if any(w in t.lower() for w in WANT):
        print(f"    *** {d}  {t}")
print("  --- first 25 titles for shape ---")
for t, d in hits[:25]:
    print(f"    {d}  {t[:78]}")
# Pagination: how many pages does the calendar expose?
for pat in (r'of\s*<?[^>]*>?\s*(\d+)\s*(?:results|releases)',
            r'"totalResults?"\s*:\s*(\d+)', r'(\d+)\s+results'):
    m = re.search(pat, body, re.I)
    if m:
        print(f"  total-ish via {pat!r}: {m.group(1)}")
