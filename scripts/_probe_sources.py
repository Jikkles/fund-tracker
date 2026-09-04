"""TEMPORARY probe v5. ONS answered 429 to four quick requests, so space
them out. Dump the real JSON rather than guessing field names: v4 guessed
releaseDate/provisionalDate and both came back None."""
import json, time, urllib.request

UA = "fund-tracker/1.0 (+github actions; personal research desk)"
B = "https://www.ons.gov.uk"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def dump(label, url, limit=2600):
    print("=" * 74)
    print(f"### {label}\n### {url}")
    try:
        payload = json.loads(get(url))
    except Exception as e:                            # noqa: BLE001 - probe
        print(f"FAILED {type(e).__name__}: {e}")
        return None
    print("top-level keys:", sorted(payload))
    if isinstance(payload.get("description"), dict):
        print("description keys:", sorted(payload["description"]))
    print(json.dumps(payload, indent=1)[:limit])
    return payload

dump("release page (upcoming CPI)",
     f"{B}/releases/consumerpriceinflationukaugust2026/data")
time.sleep(3)
dump("bulletin latest (CPI)",
     f"{B}/economy/inflationandpriceindices/bulletins/consumerpriceinflation/latest/data",
     limit=1800)
time.sleep(3)
dump("calendar page 1 as JSON",
     f"{B}/releasecalendar/data?release-type=type-upcoming", limit=2600)
