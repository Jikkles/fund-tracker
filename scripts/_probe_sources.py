"""TEMPORARY probe v4. The ONS calendar paginates at 10 a page over 343
upcoming releases, so paging it to find one monthly bulletin is 35 requests
for one date. ONS serves JSON for any page by appending /data - test whether
a bulletin's own page carries its next release date, which would make this
one request per statistic."""
import json, urllib.request

UA = "fund-tracker/1.0 (+github actions; personal research desk)"
BASE = "https://www.ons.gov.uk"
BULLETINS = {
    "CPI": "/economy/inflationandpriceindices/bulletins/consumerpriceinflation/latest",
    "Labour market": "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/bulletins/uklabourmarket/latest",
    "GDP monthly": "/economy/grossdomesticproductgdp/bulletins/gdpmonthlyestimateuk/latest",
    "Retail sales": "/businessindustryandtrade/retailindustry/bulletins/retailsales/latest",
}

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

print("########## ONS bulletin /data JSON")
for name, path in BULLETINS.items():
    url = f"{BASE}{path}/data"
    try:
        payload = json.loads(get(url))
    except Exception as e:                            # noqa: BLE001 - probe
        print(f"  -- {name}: {type(e).__name__}: {e}")
        continue
    desc = payload.get("description", {})
    print(f"  OK {name}")
    print(f"     type          = {payload.get('type')}")
    for k in ("title", "edition", "nextRelease", "releaseDate",
              "contact", "summary"):
        v = desc.get(k)
        if isinstance(v, str):
            v = v[:110]
        if k == "contact":
            v = "<contact block>" if v else None
        print(f"     {k:13s} = {v!r}")

print()
print("########## a release page's own /data (status + confirmed flag)")
# Take one upcoming release URL seen in run 1 and read its JSON.
for path in ("/releases/gdpmonthlyestimateukjuly2026",
             "/releases/consumerpriceinflationukaugust2026"):
    try:
        payload = json.loads(get(f"{BASE}{path}/data"))
    except Exception as e:                            # noqa: BLE001 - probe
        print(f"  -- {path}: {type(e).__name__}: {e}")
        continue
    d = payload.get("description", {})
    print(f"  OK {path}")
    print(f"     type={payload.get('type')} "
          f"released={d.get('published')} cancelled={d.get('cancelled')} "
          f"provisional={d.get('provisionalDate')!r}")
    print(f"     releaseDate={d.get('releaseDate')!r} title={d.get('title')!r}")
