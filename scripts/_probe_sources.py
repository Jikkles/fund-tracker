"""TEMPORARY probe v7. UK labour market resolved to no date on the live run
while CPI and GDP resolved correctly. Find out why: either the keyword
returns nothing matching the required title prefix, or the match sits past
the first page of ten."""
import html, re, time, urllib.parse, urllib.request

UA = "fund-tracker/1.0 (+github actions; personal research desk)"
CAL = "https://www.ons.gov.uk/releasecalendar"
ITEM = re.compile(
    r'data-gtm-release-title\s*=\s*"(?P<title>[^"]*)"'
    r'.*?data-gtm-release-url\s*=\s*"(?P<uri>[^"]*)"'
    r'.*?data-gtm-release-date\s*=\s*"(?P<date>\d{8})"', re.S)

def clean(raw):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(raw))).strip()

for kw in ("labour market overview", "labour market", "employment and employee types"):
    for page in (1, 2, 3):
        url = (f"{CAL}?release-type=type-upcoming"
               f"&keywords={urllib.parse.quote_plus(kw)}"
               + (f"&page={page}" if page > 1 else ""))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception as e:                        # noqa: BLE001 - probe
            print(f"  FAILED {type(e).__name__}: {e}  kw={kw!r} page={page}")
            time.sleep(2); continue
        tot = re.search(r"(\d+)\s+results", body, re.I)
        rows = [(m.group("date"), clean(m.group("title")), m.group("uri"))
                for m in ITEM.finditer(body)]
        print(f"== kw={kw!r} page={page} total={tot.group(1) if tot else '?'} n={len(rows)}")
        for d, t, u in rows:
            hit = "  <== PREFIX MATCH" if t.lower().startswith("labour market overview") else ""
            print(f"     {d}  {t[:70]}{hit}")
        time.sleep(2)
