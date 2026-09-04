"""TEMPORARY probe. Fetches candidate release-calendar sources on a runner
and prints enough markup to build real parser fixtures from. Deleted once
scripts/stat_calendar.py has fixtures captured from real responses."""
import re, sys, urllib.error, urllib.request

USER_AGENT = "fund-tracker/1.0 (+github actions; personal research desk)"

CANDIDATES = [
    ("ONS json",  "https://www.ons.gov.uk/releasecalendar/data?view=upcoming&size=50"),
    ("ONS html",  "https://www.ons.gov.uk/releasecalendar?view=upcoming&size=50"),
    ("ONS api",   "https://api.beta.ons.gov.uk/v1/releases?limit=50"),
    ("BLS sched", "https://www.bls.gov/schedule/news_release/2026_sched.htm"),
    ("BLS cpi",   "https://www.bls.gov/schedule/news_release/cpi.htm"),
    ("BLS empsit","https://www.bls.gov/schedule/news_release/empsit.htm"),
]

def main():
    for name, url in CANDIDATES:
        print("=" * 78)
        print(f"### {name}  {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                status, body = r.status, r.read().decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001 - probe
            print(f"FAILED {type(e).__name__}: {e}")
            continue
        print(f"status={status} bytes={len(body)}")
        if "json" in name or body.lstrip()[:1] in "{[":
            print(body[:6000])
            continue
        # Strip scripts/styles so the sample is markup that matters.
        t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S)
        # Print the region around the first date-shaped hit: that is where
        # the table lives, and the head of the file is all chrome.
        m = re.search(r"(January|February|March|April|May|June|July|August|"
                      r"September|October|November|December)\s+\d{1,2}", t)
        start = max(0, m.start() - 4000) if m else 0
        print(t[start:start + 9000])

if __name__ == "__main__":
    sys.exit(0 if main() is None else 1)
