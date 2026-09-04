"""TEMPORARY probe v2. Narrow the ONS query params and test whether BLS's
403 is a User-Agent block. Prints compact summaries, not raw markup."""
import re, urllib.error, urllib.request

FT_UA = "fund-tracker/1.0 (+github actions; personal research desk)"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

REL = re.compile(
    r'data-gtm-release-title\s*=\s*"([^"]*)".*?'
    r'data-gtm-release-date="(\d{8})".*?'
    r'data-gtm-release-time="([^"]*)"', re.S)

ONS_URLS = [
    "https://www.ons.gov.uk/releasecalendar/data?view=upcoming&size=50",
    "https://www.ons.gov.uk/releasecalendar?release-type=type-upcoming&size=50",
    "https://www.ons.gov.uk/releasecalendar?view=upcoming&size=50&sortBy=date-oldest",
    "https://www.ons.gov.uk/releasecalendar?release-type=type-upcoming&query=consumer%20price",
]
BLS_URLS = [
    "https://www.bls.gov/schedule/news_release/2026_sched.htm",
    "https://www.bls.gov/schedule/news_release/cpi.htm",
]

def get(url, ua):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:                            # noqa: BLE001 - probe
        return None, f"{type(e).__name__}: {e}"

print("########## ONS query shapes")
for u in ONS_URLS:
    st, body = get(u, FT_UA)
    print("-" * 70)
    print(u)
    if st is None:
        print("  FAILED", body); continue
    hits = REL.findall(body)
    print(f"  status={st} bytes={len(body)} releases={len(hits)} "
          f"json={body.lstrip()[:1] in '{['}")
    for t, d, tm in hits[:8]:
        # Status word sits in the block after the date.
        print(f"    {d} {tm}  {t[:72]}")
    for word in ("Provisional", "Confirmed", "Published", "Cancelled"):
        print(f"    [{word}: {body.count(word)}]", end="")
    print()

print()
print("########## BLS user-agent test")
for u in BLS_URLS:
    for name, ua in (("ft-ua", FT_UA), ("browser-ua", BROWSER_UA)):
        st, body = get(u, ua)
        print(f"  {name:11s} {u.rsplit('/',1)[-1]:16s} -> "
              f"{st if st else body[:60]}"
              + (f" bytes={len(body)}" if st else ""))
