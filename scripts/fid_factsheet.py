"""
Refresh the researched comparators from each fund's Fidelity factsheet.

WHY THIS EXISTS
---------------
For months the desk said, in its caveats and its maintenance report, that the
sector column beside a fund's 1/3/5yr figures "ages, because nothing free
publishes those", and asked a person to re-research it. That was wrong.
Fidelity's factsheet pages publish, server-rendered, everything that list was
waiting on:

  * trailing 1/3/5yr returns with a Morningstar category average beside them
  * five discrete years to 30 September, fund and category
  * the SRRI as collected from the fund's KIID, with the date collected
  * the transaction cost, the comparative index (benchmark) and the yields

A hand sweep on 8 Oct 2026 took 23 stale and 5 undated tables to none. This
module is that sweep, run weekly after hl_factsheet.py so it does not happen
once and then age again.

WHAT IT REFUSES TO DO
---------------------
Fidelity will happily serve a different share class from the one HL lists,
and a figure for the wrong class is precise and wrong. So nothing is applied
unless every gate below passes, and a fund that fails one is reported and left
exactly as it was:

  1. IDENTITY. The Fidelity page's own SEDOL must equal the SEDOL HL's page
     names for the class this desk tracks (stored by hl_factsheet.py). The
     ISIN is derived from that SEDOL - GB/IE + "00" + SEDOL + check digit -
     and is a candidate only; the SEDOL match is the proof.
  2. SAME FUND, SAME NUMBERS. Fidelity's 1yr return must be within 2pp of the
     desk's own NAV-computed 1yr. This caught Polar Capital European Income,
     whose Fidelity page is a different class at +6% against the desk's +11%.
  3. A REAL DATE. Fidelity's trailing table states no as-at date. It runs to
     the page's price stamp, which is proved per fund rather than assumed:
     the 1-day return must equal the stamped price change, or - where
     Fidelity has no 1-day figure - the desk's own NAV must be priced to the
     same day with matching 1-week and YTD returns. Where the two are priced
     to the same day their YTDs must also agree. Fail and the trailing table
     is not written - an undated table is exactly what this replaced.
  4. NO ZERO-AS-DATA. Fidelity prints an exact "0" for a transaction cost or
     yield it has not got; real figures carry precision ("0.582", "-0.02").
     A bare zero is treated as absent, never written as 0.00%.

The comparator is the Morningstar category average, not the IA sector
average, and every note says so. Bases are not like-for-like with HL or FE.

  python scripts/fid_factsheet.py                 # refresh every fund
  python scripts/fid_factsheet.py --dry-run       # report, write nothing
  python scripts/fid_factsheet.py --only <id> ... # named funds only
  python scripts/fid_factsheet.py --selftest      # offline checks
"""

from __future__ import annotations

import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import netfetch
import perf_dates

FUNDS = Path(__file__).resolve().parent.parent / "data" / "funds.json"
PAGE = "https://www.fidelity.co.uk/factsheet-data/factsheet/{isin}/{tab}"
NYV = "not yet verified"
DASH = "—"

ONE_YEAR_TOLERANCE_PP = 2.0
YTD_TOLERANCE_PP = 1.0
# The desk stores NAV returns to 2dp, so "the same figure" allows rounding.
NAV_MATCH_PP = 0.06
PRICE_STAMP_MAX_AGE_DAYS = 7
SRRI_MAX_AGE_DAYS = 400
# A shorter Fidelity history replaces a longer researched one only when it is
# at least this much newer - two new years are not worth losing five old ones.
DISCRETE_SHORTER_MIN_NEWER_DAYS = 180

# Everything up to and including this sentence is written by this module and
# regenerated each run; anything a person adds after it is kept.
NOTE_END = "not like-for-like with HL or FE tables."


# ------------------------------------------------------------------ helpers

def isin_from_sedol(prefix: str, sedol: str) -> str:
    """ISIN for a SEDOL-coded security: country + "00" + SEDOL + Luhn digit."""
    body = prefix + "00" + sedol
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return body + str((10 - total % 10) % 10)


def num(s) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def pct(v: float) -> str:
    return ("+" if v >= 0 else "−") + f"{abs(v):.2f}%"


def parse_pct(s) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace("−", "-").replace("%", "").replace("+", ""))
    except ValueError:
        return None


def unverified(v) -> bool:
    return v is None or NYV in str(v).lower()


def stamp(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def short(d: date) -> str:
    return f"{d.day} {d:%b %y}"


# ------------------------------------------------------------------ fetching

def page_state(isin: str, tab: str) -> dict | None:
    """The fund state Fidelity embeds in one tab, or None."""
    r = netfetch.fetch(PAGE.format(isin=isin, tab=tab), ua=netfetch.UA_BROWSER,
                       timeout=40)
    if not r:
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None
    try:
        st = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["fund"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    return None if st.get("isError") else st


def resolve(fund: dict) -> dict:
    """Fetch everything apply() needs. Never raises; says why on failure."""
    sedol = fund.get("sedol")
    if not sedol:
        return {"error": "no SEDOL on file (hl_factsheet.py records it)"}
    cands = [isin_from_sedol(p, sedol) for p in ("GB", "IE")]
    if fund.get("isin") and fund["isin"] not in cands:
        cands.append(fund["isin"])
    for isin in cands:
        ks = page_state(isin, "key-statistics")
        if not ks:
            continue
        pf = page_state(isin, "performance") or {}
        rr = page_state(isin, "risk-and-rating") or {}
        return {"isin": isin, "sedol": sedol,
                "keyStats": ks.get("keyStats") or {},
                "fundData": ks.get("fundData") or {},
                "performance": pf.get("performance") or {},
                "priceDtls": pf.get("priceDtls") or {},
                "riskAndRating": rr.get("riskAndRating") or {}}
    return {"error": f"no Fidelity page for SEDOL {sedol}"}


# ------------------------------------------------------------------ applying

def apply(fund: dict, rec: dict, today: date) -> tuple[list[str], str | None]:
    """Write what passes the gates. Returns (changes, reason-if-refused)."""
    if rec.get("error"):
        return [], rec["error"]
    fd = rec["fundData"]
    if fd.get("sedol") != rec["sedol"]:
        return [], (f"class mismatch: Fidelity page is {fd.get('name')!r} "
                    f"(SEDOL {fd.get('sedol')}), HL lists SEDOL {rec['sedol']}")

    perf = fund.setdefault("performance", {})
    trail = {t.get("timeframe"): t for t in rec["performance"].get("timeFrameData") or []}

    def tr(tf, key="trailingReturnsValue"):
        return num((trail.get(tf) or {}).get(key))

    m12, nav1 = tr("M12"), parse_pct(perf.get("nav1yr"))
    if m12 is not None and nav1 is not None and abs(m12 - nav1) > ONE_YEAR_TOLERANCE_PP:
        return [], (f"1yr check failed: Fidelity {m12:+.2f}% vs desk NAV "
                    f"{nav1:+.2f}% ({fd.get('name')})")

    read = stamp(today)
    changes: list[str] = []
    notes: list[str] = []
    if not fund.get("isin"):
        fund["isin"] = rec["isin"]
        fund["isinSource"] = (f"derived from HL SEDOL {rec['sedol']}, confirmed "
                              f"on Fidelity factsheet {read}")
        changes.append(f"isin: {rec['isin']}")
    fund.setdefault("links", {}).setdefault(
        "fidelity", PAGE.format(isin=rec["isin"], tab="performance"))
    cat = (rec["performance"].get("performanceApiHeaders") or {}).get("categoryname") or "category"

    # ---- trailing (cumulative) table, only with a proven date
    pd = rec["priceDtls"]
    asof = None
    if pd.get("lastUpdated"):
        try:
            asof = datetime.strptime(pd["lastUpdated"][:10], "%Y-%m-%d").date()
        except ValueError:
            asof = None
    d1, chg = tr("D1"), num(pd.get("changePercentage"))
    ytd, navytd = tr("M0"), parse_pct(perf.get("navYtd"))
    w1, navw1 = tr("W1"), parse_pct(perf.get("nav1w"))
    # The desk's NAV and Fidelity's page can be priced to different days -
    # Yahoo runs a day behind on some Vanguard lines - and a YTD compared
    # across two end dates differs by that day's move, not by any fault.
    same_day = bool(asof) and perf.get("navAsAt") == asof.isoformat()
    # Two ways to prove the trailing table's date, either sufficient:
    #  - its 1-day return is the price change stamped beside it, or
    #  - the desk's own NAV is priced to the same day and its 1-week and YTD
    #    agree to within rounding. Fidelity publishes no 1-day figure for
    #    some funds, and on a weekday its price stamp can move before its
    #    trailing table does; the NAV route covers both.
    d1_ok = d1 is not None and chg is not None and abs(d1 - chg) < 0.02
    nav_ok = (same_day and None not in (w1, navw1, ytd, navytd)
              and abs(w1 - navw1) <= NAV_MATCH_PP and abs(ytd - navytd) <= NAV_MATCH_PP)
    why_not = None
    if m12 is None:
        why_not = "no 1yr figure"
    elif asof is None or (today - asof).days > PRICE_STAMP_MAX_AGE_DAYS:
        why_not = f"price stamp {asof} too old or missing"
    elif not (d1_ok or nav_ok):
        why_not = (f"1-day {d1} does not match stamped price change {chg}, and "
                   f"the desk's NAV ({perf.get('navAsAt')}) does not match it either")
    elif same_day and ytd is not None and navytd is not None and abs(ytd - navytd) > YTD_TOLERANCE_PP:
        why_not = f"YTD {ytd:+.2f}% vs desk NAV {navytd:+.2f}%"
    cum_written = False
    if why_not is None:
        rows = []
        for tf, label in (("M12", "1 yr (trailing, to {})"),
                          ("M36", "3 yr (annualised, to {})"),
                          ("M60", "5 yr (annualised, to {})")):
            v, s = tr(tf), tr(tf, "trailingReturnsBenchmarkValue")
            if v is not None:
                rows.append({"period": label.format(short(asof)), "fund": pct(v),
                             "sector": pct(s) if s is not None else DASH,
                             "benchmark": DASH})
        if perf.get("cumulative") != rows:
            changes.append(f"cumulative to {short(asof)}")
        perf["cumulative"] = rows
        perf["perfBasis"] = "FID"
        cum_written = True
        notes.append(f"trailing figures to {stamp(asof)} (1/3/5yr; 3 and 5yr annualised)")
    else:
        changes.append(f"cumulative NOT refreshed: {why_not}")

    # ---- discrete years
    yd = sorted(rec["performance"].get("yearlyData") or [],
                key=lambda y: y.get("endDate", ""), reverse=True)[:5]
    yd = [y for y in yd if num(y.get("annualPerformanceValue")) is not None]
    if yd:
        rows = []
        for y in yd:
            sd = datetime.strptime(y["startDate"], "%Y-%m-%d")
            ed = datetime.strptime(y["endDate"], "%Y-%m-%d")
            s = num(y.get("annualPerformanceBenchmarkValue"))
            rows.append({"year": f"{sd:%d/%m/%y} to {ed:%d/%m/%y}",
                         "fund": pct(num(y["annualPerformanceValue"])),
                         "sector": pct(s) if s is not None else NYV})
        newest = datetime.strptime(yd[0]["endDate"], "%Y-%m-%d").date()
        old = perf.get("discrete") or []
        old_has_sector = any(not unverified(r.get("sector")) and r.get("sector") != DASH
                             for r in old)
        old_asat = perf.get("discreteAsAt")
        take = any(r["sector"] != NYV for r in rows)
        if take and old_has_sector and old_asat:
            gap = (newest - date.fromisoformat(old_asat)).days
            if gap < 0 or (len(rows) < len(old) and gap < DISCRETE_SHORTER_MIN_NEWER_DAYS):
                take = False
                changes.append(f"discrete kept: Fidelity has {len(rows)} years to "
                               f"{newest}, stored has {len(old)} to {old_asat}")
        if take:
            if old != rows:
                changes.append(f"discrete to {newest:%d %b %y}")
            perf["discrete"] = rows
            notes.append(f"discrete years to {stamp(newest)}")

    if notes:
        note = (f"Refreshed {read} from Fidelity's factsheet ({fd.get('name')}, "
                f"{rec['isin']}): " + "; ".join(notes) + ".")
        if not cum_written and perf.get("cumulative"):
            note += (" The cumulative table above is NOT from this refresh: "
                     f"Fidelity's trailing figures could not be dated ({why_not}).")
        note += (f" Sector column is the Morningstar {cat} category average as "
                 "Fidelity publishes it, not the IA sector average - " + NOTE_END)
        old_note = perf.get("notes") or ""
        if NOTE_END in old_note and old_note.startswith("Refreshed "):
            tail = old_note.split(NOTE_END, 1)[1]
        elif old_note and not cum_written and perf.get("cumulative"):
            tail = " Earlier note: " + old_note
        else:
            tail = ""
            if old_note:
                changes.append("notes replaced (described the superseded tables)")
        # An earlier note describes the cumulative table it was written with;
        # once that table is replaced the note is about nothing on the card.
        if cum_written and tail.startswith(" Earlier note: "):
            tail = ""
        perf["notes"] = note + tail
        perf_dates.stamp(fund, today)
        if "Factsheet depth not yet verified." in fund.get("sources", ""):
            fund["sources"] = fund["sources"].replace(
                "Factsheet depth not yet verified.",
                f"Performance comparators, SRRI and transaction cost from "
                f"Fidelity's factsheet ({rec['isin']}).")

    # ---- SRRI, as collected from the KIID
    srri = rec["riskAndRating"].get("collectedSRRI") or {}
    rank = str(srri.get("rank") or "")
    if rank in {"1", "2", "3", "4", "5", "6", "7"} and srri.get("date"):
        sd = date.fromisoformat(srri["date"][:10])
        if (today - sd).days <= SRRI_MAX_AGE_DAYS:
            risk = fund.setdefault("risk", {})
            if str(risk.get("srri")) != rank:
                changes.append(f"srri: {risk.get('srri')!r} -> {rank}")
            risk["srri"] = rank
            risk["srriConfidence"] = "confirmed"
            risk["srriNote"] = f"Fidelity factsheet, collected SRRI dated {stamp(sd)}"

    # ---- charges, benchmark, yield
    ks = rec["keyStats"]
    raw_tc = ks.get("transactionCost")
    if raw_tc not in (None, "", "0") and num(raw_tc) is not None:
        ch = fund.setdefault("charges", {})
        new = f"{num(raw_tc):.2f}%"
        if ch.get("transaction") != new:
            changes.append(f"transaction: {ch.get('transaction')!r} -> {new}")
        ch["transaction"] = new
        ch["transactionSource"] = "confirmed-fidelity"
    if unverified(fund.get("benchmark")) and ks.get("fundComparativeIndex"):
        fund["benchmark"] = ks["fundComparativeIndex"]
        fund["benchmarkSource"] = f"Fidelity factsheet (fund comparative index), read {read}"
        changes.append(f"benchmark: {ks['fundComparativeIndex']}")
    if unverified(fund.get("yield")):
        for key, basis in (("historicYield", "historic"),
                           ("distributionYield", "distribution"),
                           ("underlyingYield", "underlying")):
            if ks.get(key) not in (None, "", "0") and num(ks.get(key)):
                fund["yield"] = f"{num(ks[key]):.2f}% ({basis}, Fidelity {read})"
                changes.append(f"yield: {fund['yield']}")
                break
    return changes, None


# ------------------------------------------------------------------ main

def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    only = set(argv[argv.index("--only") + 1:]) if "--only" in argv else set()
    doc = json.load(io.open(FUNDS, encoding="utf-8"))
    funds = [f for f in doc["funds"] if not only or f["id"] in only]
    today = date.today()

    # The netfetch rate limit keeps this polite; the workers hide latency,
    # which on these pages is several seconds a request.
    with ThreadPoolExecutor(6) as pool:
        recs = list(pool.map(resolve, funds))

    refused = 0
    for fund, rec in zip(funds, recs):
        changes, why = apply(fund, rec, today)
        if why:
            refused += 1
            print(f"  SKIP {fund['id']}: {why}")
        else:
            print(f"  ok   {fund['id']}: {'; '.join(changes) or 'unchanged'}")
    print(f"\n{len(funds) - refused} of {len(funds)} funds read from Fidelity; "
          f"{refused} refused and left as they were.")
    if dry:
        print("--dry-run: nothing written")
        return 0
    io.open(FUNDS, "w", encoding="utf-8", newline="\n").write(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {FUNDS}")
    return 0


# ------------------------------------------------------------------ self-test

def _fixture(**over) -> tuple[dict, dict]:
    fund = {"id": "x", "performance": {"nav1yr": "+29.50%", "navYtd": "+22.78%",
                                        "nav1w": "-0.05%", "navAsAt": "2026-10-07",
                                        "discrete": [{"year": "02/10/25 to 02/10/26",
                                                      "fund": "+1.00%", "sector": NYV}]},
            "risk": {"srri": "6"}, "charges": {"transaction": NYV},
            "benchmark": NYV, "yield": NYV, "sedol": "B5ZX1M7"}
    years = [{"startDate": f"{y - 1}-09-30", "endDate": f"{y}-09-30",
              "annualPerformanceValue": str(v), "annualPerformanceBenchmarkValue": str(s)}
             for y, v, s in ((2026, 31.01, 14.56), (2025, 43.92, 10.22),
                             (2024, 24.94, 13.83), (2023, 9.05, 7.40), (2022, -3.5, 0.26))]
    rec = {"isin": "GB00B5ZX1M70", "sedol": "B5ZX1M7",
           "fundData": {"sedol": "B5ZX1M7", "name": "Example I Acc"},
           "keyStats": {"transactionCost": "0.582", "fundComparativeIndex": "MSCI ACWI NR GBP",
                        "historicYield": "2.34"},
           "priceDtls": {"lastUpdated": "2026-10-07 01:00:00", "changePercentage": "-1.09"},
           "performance": {"performanceApiHeaders": {"categoryname": "Global Equity Income"},
                           "yearlyData": years,
                           "timeFrameData": [
                               {"timeframe": "D1", "trailingReturnsValue": "-1.089975"},
                               {"timeframe": "W1", "trailingReturnsValue": "-0.051446"},
                               {"timeframe": "M0", "trailingReturnsValue": "22.78"},
                               {"timeframe": "M12", "trailingReturnsValue": "29.69",
                                "trailingReturnsBenchmarkValue": "13.62"},
                               {"timeframe": "M36", "trailingReturnsValue": "34.42",
                                "trailingReturnsBenchmarkValue": "13.70"},
                               {"timeframe": "M60", "trailingReturnsValue": "21.35",
                                "trailingReturnsBenchmarkValue": "-0.5"}]},
           "riskAndRating": {"collectedSRRI": {"date": "2026-09-30T00:00:00", "rank": "5"}}}
    for k, v in over.items():
        rec[k] = v
    return fund, rec


def _selftest() -> None:
    today = date(2026, 10, 8)
    assert isin_from_sedol("GB", "B5ZX1M7") == "GB00B5ZX1M70"
    assert isin_from_sedol("IE", "BF2N1T7") == "IE00BF2N1T73"
    assert isin_from_sedol("GB", "B2PLJL5") == "GB00B2PLJL57"
    print("  isin check digit OK", file=sys.stderr)

    fund, rec = _fixture()
    changes, why = apply(fund, rec, today)
    assert why is None, why
    p = fund["performance"]
    assert p["cumulative"][0] == {"period": "1 yr (trailing, to 7 Oct 26)", "fund": "+29.69%",
                                  "sector": "+13.62%", "benchmark": DASH}, p["cumulative"]
    assert p["cumulative"][2]["sector"] == "−0.50%", "sign must survive"
    assert p["perfAsAt"] == "2026-10-07" and p["perfBasis"] == "FID"
    assert p["discrete"][0]["year"] == "30/09/25 to 30/09/26"
    assert p["discrete"][4]["fund"] == "−3.50%"
    assert p["discreteAsAt"] == "2026-09-30" and p["discreteMissingYears"] == 0
    assert fund["risk"]["srri"] == "5" and fund["charges"]["transaction"] == "0.58%"
    assert fund["benchmark"] == "MSCI ACWI NR GBP" and fund["isin"] == "GB00B5ZX1M70"
    assert fund["yield"].startswith("2.34% (historic")
    assert "Morningstar Global Equity Income category" in p["notes"]
    print("  happy path       OK", file=sys.stderr)

    # A person's addition after the generated note survives the next run.
    p["notes"] += " Manager changed 1 Oct."
    apply(fund, rec, today)
    assert p["notes"].endswith(NOTE_END + " Manager changed 1 Oct."), p["notes"]
    print("  note tail kept   OK", file=sys.stderr)

    # Gate 1: a page for another class is refused outright, nothing written.
    fund, rec = _fixture(fundData={"sedol": "B1XBN52", "name": "Other class"})
    before = json.dumps(fund, sort_keys=True)
    changes, why = apply(fund, rec, today)
    assert why and "class mismatch" in why and json.dumps(fund, sort_keys=True) == before
    # Gate 2: same SEDOL but the numbers are not the desk's fund.
    fund, rec = _fixture()
    fund["performance"]["nav1yr"] = "+10.73%"
    before = json.dumps(fund, sort_keys=True)
    changes, why = apply(fund, rec, today)
    assert why and "1yr check" in why and json.dumps(fund, sort_keys=True) == before
    print("  identity gates   OK", file=sys.stderr)

    # Gate 3: an undatable trailing table is not written, the rest still is,
    # and a person's earlier note on the kept table is preserved.
    fund, rec = _fixture(priceDtls={"lastUpdated": "2026-10-07 01:00:00",
                                    "changePercentage": "0.00"})
    fund["performance"]["navAsAt"] = "2026-10-06"     # so the NAV route cannot vouch either
    fund["performance"]["cumulative"] = [{"period": "1 yr (to 20 May 26)", "fund": "+1%",
                                          "sector": DASH, "benchmark": DASH}]
    fund["performance"]["notes"] = "FE Analytics, 20 May."
    changes, why = apply(fund, rec, today)
    p = fund["performance"]
    assert p["cumulative"][0]["period"] == "1 yr (to 20 May 26)"
    assert "NOT from this refresh" in p["notes"] and p["notes"].endswith("Earlier note: FE Analytics, 20 May.")
    assert p["discrete"][0]["year"] == "30/09/25 to 30/09/26"
    fund, rec = _fixture()
    fund["performance"]["navYtd"] = "+25.00%"
    apply(fund, rec, today)
    assert "cumulative" not in fund["performance"], "same-day YTD disagreement must refuse"
    # ...but a desk NAV a day behind is a different window, not a disagreement.
    fund, rec = _fixture()
    fund["performance"].update(navYtd="+23.80%", navAsAt="2026-10-06")
    apply(fund, rec, today)
    assert fund["performance"]["cumulative"][0]["period"].endswith("7 Oct 26)")
    # No 1-day figure at all: the desk's same-day NAV can still prove the date...
    no_d1 = [t for t in _fixture()[1]["performance"]["timeFrameData"] if t["timeframe"] != "D1"]
    fund, rec = _fixture()
    rec["performance"]["timeFrameData"] = no_d1
    apply(fund, rec, today)
    assert "cumulative" in fund["performance"], "NAV route must date a table with no D1"
    # ...but only if its week matches too.
    fund, rec = _fixture()
    rec["performance"]["timeFrameData"] = no_d1
    fund["performance"]["nav1w"] = "+0.40%"
    apply(fund, rec, today)
    assert "cumulative" not in fund["performance"]
    print("  dating gates     OK", file=sys.stderr)

    # Gate 4: a bare zero is Fidelity's blank, not a figure.
    fund, rec = _fixture(keyStats={"transactionCost": "0", "historicYield": "0"})
    apply(fund, rec, today)
    assert fund["charges"]["transaction"] == NYV and fund["yield"] == NYV
    print("  zero-as-blank    OK", file=sys.stderr)

    # A shorter Fidelity history does not replace a longer researched one
    # that is only a few months older.
    fund, rec = _fixture()
    rec["performance"]["yearlyData"] = rec["performance"]["yearlyData"][:2]
    fund["performance"]["discrete"] = [{"year": f"y{i}", "fund": "+1%", "sector": "+2%"}
                                       for i in range(5)]
    fund["performance"]["discreteAsAt"] = "2026-06-30"
    apply(fund, rec, today)
    assert fund["performance"]["discrete"][0]["year"] == "y0"
    print("  discrete keep    OK", file=sys.stderr)
    print("  fid_factsheet self-test: OK", file=sys.stderr)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        raise SystemExit(0)
    raise SystemExit(main(sys.argv[1:]))
