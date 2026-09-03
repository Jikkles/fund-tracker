"""
Daily fund desk update - deterministic, no LLM, no API key, no cost.

What this claims and what it does not
-------------------------------------
Where fund_nav.py resolved the fund's OWN published NAV series, that figure
wins - it is the fund's actual return rather than a stand-in. That script runs
ahead of this one in the workflow, so its numbers are already on disk here.

Failing that, for an INDEX TRACKER fund it computes a real figure from a real
priced instrument: a London-listed GBP ETF tracking the same index. That is not
an estimate, it is arithmetic on market data, and it beats a model guess because
it embeds the currency effect a UK holder actually experiences.

Only when neither is available does a fund get "not yet verified", with the real
index moves for its market attached. It does not invent a fund-level figure,
because a plausible guess is worse than an honest gap.

That ladder is the design. The desk stays honest by construction rather than by
instruction - there is no prompt to disregard and no model to drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import calendar_data as cal
import market_data as md
import perf_dates
from proxies import CONTEXT_TICKERS, FUND_PROXIES, GROUP_CONTEXT

ROOT = Path(__file__).resolve().parent.parent
FUNDS = ROOT / "data" / "funds.json"
AUDIT_TRAIL_MAX = 30      # keep the last month of run records, not all time
REPORT = ROOT / "data" / "last_run.md"

WINDOW_DAYS = 30

# How stale a stored NAV figure may be before it stops counting as today's
# number. NAV series lag by a day or two normally; 8 covers a long weekend
# plus a bank holiday without ever letting last month's figure pose as fresh.
NAV_MAX_AGE_DAYS = 8

# How old hand-researched factsheet data may get before the run flags it.
# Factsheets refresh monthly or quarterly, so 120 days means at least one full
# quarter was missed. Nothing here can fix it - only a person can - so the
# warning exists to turn a silent drift into a visible worklist.
RESEARCH_STALE_DAYS = 120

# Caveats that hold regardless of what any given run produced. Anything that
# depends on the data - dates, counts, which basis a figure used - belongs in
# build_caveats() instead: a hand-written claim about the data starts drifting
# out of date the moment the data moves, and the reader has no way to tell.
DURABLE_CAVEATS = [
    "Factual research and catalyst-based ideas to investigate — not personal "
    "financial advice.",
    "Past performance is not a guide to future returns; investments and income "
    "can fall as well as rise.",
    "Where a field shows 'not yet verified' it was not confirmed within the "
    "research run — not invented or omitted by choice.",
    "Portfolio breakdowns (holdings, sectors, countries) are point-in-time from "
    "each fund's last published factsheet and are older than its price data. "
    "Every fund's holdings note carries its own as-at date.",
    "Wealth Shortlist status is read from HL's own Wealth Shortlist data, not "
    "inferred from factsheet prose. A fund marked Ex-Shortlist was dropped by HL "
    "and is still tracked here; funds with neither badge cover asset classes the "
    "Shortlist does not include. Membership is HL's view, not a rating by this desk.",
]

CONF_TEXT = {
    "exact": "computed from a same-index GBP ETF proxy - verify vs HL factsheet",
    "close": "computed from a similar-index GBP ETF proxy - verify vs HL factsheet",
    "loose": "computed from a loose GBP ETF proxy - treat as directional only",
}


def stamp(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def context_line(quotes: dict[str, md.Quote], labels: list[str]) -> str:
    return ", ".join(f"{lbl} {quotes[lbl].format_pct()}"
                     for lbl in labels if lbl in quotes)


def nav_entry(fund: dict, today: date, ctx: str) -> dict | None:
    """The fund's own NAV return, when fund_nav.py left a fresh one on disk.

    A published NAV is the fund's actual result, so it outranks a proxy. The
    page already prefers performance.nav1m when rendering; leaving oneMonth
    saying "not yet verified" underneath it made the audit report contradict
    the page it describes. Returns None when there is no figure or it has aged
    out, dropping the fund through to the proxy rung below.
    """
    perf = fund.get("performance") or {}
    value, as_at = perf.get("nav1m"), perf.get("navAsAt")
    if not value or not as_at:
        return None
    try:
        priced = date.fromisoformat(as_at)
    except ValueError:
        return None
    if not 0 <= (today - priced).days <= NAV_MAX_AGE_DAYS:
        return None

    # Share-class and redenomination caveats travel with the figure - they are
    # the reason a reader might not want to take it at face value.
    caveats = " ".join(fund[k] for k in ("navClassNote", "navNote")
                       if fund.get(k))
    return {
        "value": value,
        "note": (
            f"Total return over the 31 days to {stamp(priced)}, computed from "
            f"this fund's own published NAV series - not a proxy and not an "
            f"estimate. "
            + (f"{caveats} " if caveats else "")
            + (f"Market context: {ctx}." if ctx else "")
        ).strip(),
        "confidence": "computed from the fund's own published NAV",
        "asAt": stamp(priced),
        "basis": f"nav:{fund.get('navSymbol', '')}",
        "source": "Yahoo Finance (fund NAV series)",
    }


def build_one_month(doc: dict, quotes: dict[str, md.Quote],
                    proxy_quotes: dict[str, md.Quote],
                    today: date, window_start: date) -> tuple[dict, dict]:
    entries: dict[str, dict] = {}
    stats = {"nav": 0, "nav_stale": 0,
             "computed": 0, "unverified": 0, "proxy_failed": 0}
    window = f"{stamp(window_start)} to {stamp(today)}"

    for fund in doc["funds"]:
        fid, group = fund["id"], fund["group"]
        proxy = FUND_PROXIES.get(fid)
        quote = proxy_quotes.get(fid) if proxy else None
        ctx = context_line(quotes, GROUP_CONTEXT.get(group, []))

        own = nav_entry(fund, today, ctx)
        if own:
            entries[fid] = own
            stats["nav"] += 1
            continue

        if (fund.get("performance") or {}).get("nav1m"):
            # A NAV figure exists but is too old to stand as today's number.
            stats["nav_stale"] += 1

        if proxy and quote:
            entries[fid] = {
                "value": quote.format_pct(),
                "note": (
                    f"Computed over {window} from {proxy.label} "
                    f"({quote.symbol}), which tracks the same index this fund "
                    f"follows. Priced in GBP, so the figure already includes "
                    f"any currency effect a UK holder experiences. "
                    + (f"{proxy.note} " if proxy.note else "")
                    + (f"Market context: {ctx}." if ctx else "")
                ).strip(),
                "confidence": CONF_TEXT[proxy.confidence],
                "asAt": stamp(today),
                "basis": f"proxy:{quote.symbol}",
                "source": quote.source,
            }
            stats["computed"] += 1
            continue

        if proxy and not quote:
            stats["proxy_failed"] += 1

        reason = ("proxy pricing failed this run" if proxy else
                  "actively managed - no free source publishes fund-level returns")
        entries[fid] = {
            "value": "not yet verified",
            "note": (
                f"No fund-level figure available ({reason}). "
                + (f"For context, over {window}: {ctx}. " if ctx else "")
                + "This is market context for the fund's universe, NOT this "
                  "fund's return - an active manager can diverge sharply from "
                  "its index in either direction."
            ),
            "confidence": "not verified - market context only",
            "asAt": stamp(today),
        }
        stats["unverified"] += 1

    return entries, stats


def plural(n: int, one: str, many: str) -> str:
    """Counts here swing between 1 and 50 run to run - agree the verb to them."""
    return one if n == 1 else many


def build_caveats(doc: dict, entries: dict, stats: dict,
                  today: date) -> list[str]:
    """Rebuild the data-dependent caveats from what this run actually did.

    These are rendered on the page. Written by hand they went stale silently -
    the list was still telling readers prices were "mostly 15-22 Jun 2026"
    months after the daily NAV pricing landed. Derived from the data, they
    cannot say anything the run did not just do.
    """
    funds = doc["funds"]
    total = len(funds)
    lines: list[str] = []

    nav_dates = sorted(
        (f.get("performance") or {}).get("navAsAt")
        for f in funds
        if (entries.get(f["id"]) or {}).get("basis", "").startswith("nav:")
        and (f.get("performance") or {}).get("navAsAt"))
    if nav_dates:
        first = date.fromisoformat(nav_dates[0])
        last = date.fromisoformat(nav_dates[-1])
        span = (f"priced {stamp(last)}" if first == last
                else f"priced {stamp(first)} to {stamp(last)}")
        lines.append(
            f"{stats['nav']} of {total} funds "
            f"{plural(stats['nav'], 'carries', 'carry')} a 1-month figure "
            f"computed from the fund's own published NAV series ({span}), "
            f"refreshed every run. {stats['computed']} "
            f"{plural(stats['computed'], 'uses', 'use')} a same-index GBP ETF "
            f"proxy and {stats['unverified']} "
            f"{plural(stats['unverified'], 'carries', 'carry')} no fund-level "
            f"figure at all - each figure is labelled with the basis it used.")

    # Two tables, two clocks - see research_health. The trailing figures age
    # by the calendar; the discrete years are completed history and age only
    # when a period closes without being added. The first cut of this caveat
    # reported both on one clock and put fourteen complete annual tables on a
    # staleness list, which is the same fault it was written to fix, inverted.
    cum_funds = [f for f in funds if (f.get("performance") or {}).get("cumulative")]
    dis_funds = [f for f in funds if (f.get("performance") or {}).get("discrete")]
    cum_dates = sorted(d for f in cum_funds
                       if (d := (f.get("performance") or {}).get("perfAsAt")))
    undated = len(cum_funds) - len(cum_dates)
    if cum_dates:
        oldest, newest = (date.fromisoformat(cum_dates[0]),
                          date.fromisoformat(cum_dates[-1]))
        run = (f"are all as at {stamp(oldest)}" if oldest == newest
               else f"run {stamp(oldest)} to {stamp(newest)}")
        rest = (f" A further {undated} state no date and their age is unknown."
                if undated else "")
        lines.append(
            f"{len(cum_funds)} of {total} funds carry a hand-entered "
            f"cumulative table of trailing 1/3/5yr figures, which the daily "
            f"run does NOT refresh. Read from their own period labels, the "
            f"{len(cum_dates)} that state a date {run}, up to "
            f"{(today - oldest).days} days old.{rest} The fund's own totals "
            f"over those windows are computed from its NAV series every run "
            f"and carry the NAV chip; it is the sector and benchmark "
            f"comparators beside them that age, because nothing free "
            f"publishes those. Bases differ (Trustnet/FE, HL rolling "
            f"12-month, Fidelity annualised) — do not compare across funds "
            f"without adjusting.")

    if dis_funds:
        behind = sum(1 for f in dis_funds
                     if (f.get("performance") or {}).get("discreteMissingYears"))
        bases = sorted({b for f in dis_funds
                        if (b := (f.get("performance") or {}).get("discreteBasis"))})
        state = (f"{behind} are missing a period that has since closed"
                 if behind else
                 "every one carries the most recent year that has closed on "
                 "its own basis")
        lines.append(
            f"{len(dis_funds)} funds carry discrete annual returns, kept to "
            f"a fixed year-end ({', '.join(bases)}) rather than to the run "
            f"date: {state}. A completed year is history and does not go out "
            f"of date on the calendar, so these are not aged in days.")

    derived = sum(1 for f in funds
                  if (f.get("performance") or {}).get("perfBasis") == "DRV")
    if derived:
        whose = "fund's" if derived == 1 else "funds'"
        lines.append(
            f"{derived} {whose} cumulative 3/5yr figures are derived by "
            f"compounding discrete annual returns and are approximate — "
            f"verify against Trustnet or Morningstar for published totals.")

    return DURABLE_CAVEATS + lines


def research_health(doc: dict, today: date) -> list[str]:
    """Warn where hand-researched factsheet data has genuinely gone out of date.

    calendar_data.calendar_health does this for the central bank tables. The
    researched half of each card ages the same way but had nothing watching it,
    so it aged in silence while the NAV figures beside it stayed current - the
    widest gap on the desk between what is fresh and what looks fresh.

    It then spent months unable to fire at all, because perfAsAt was stamped
    with the date of each factsheet read and the weekly refresh reset all 103
    every Sunday: a watchdog wired to a clock reset faster than it could run.
    Fixing that overcorrected in the other direction, flagging 19 funds when
    only 10 had anything wrong, because it aged both tables on one clock.

    They need two, and the distinction is the whole of this function now:

      * a CUMULATIVE table is trailing. "1 yr (trailing, to 26 Jun 26)" claims
        to be the last twelve months and is less true every day. Days since
        measured is the right question, and 120 of them means a quarter was
        missed.
      * a DISCRETE table is completed annual periods on a fixed basis. The
        year to 31 Mar 2026 is a historical fact that does not decay, and the
        next one on that basis does not close until 31 Mar 2027. Asking how
        many days old it is puts fourteen funds on a worklist with nothing to
        do. The right question is whether a year has closed without being
        added - which today, across all 96 tables, none has.
    """
    aged: list[tuple[int, str]] = []
    behind: list[tuple[int, str]] = []
    unknown: list[str] = []
    for fund in doc["funds"]:
        perf = fund.get("performance") or {}
        # A fund with no researched tables has no research to age. Seven run
        # on their NAV series alone, and telling their owner to re-research a
        # table is a worklist item for a table that does not exist.
        if not perf.get("cumulative") and not perf.get("discrete"):
            continue

        if perf.get("cumulative"):
            as_at = perf.get("perfAsAt")
            try:
                age = (today - date.fromisoformat(as_at)).days if as_at else None
            except ValueError:
                age = None
            if age is None:
                unknown.append(fund["id"])
            elif age > RESEARCH_STALE_DAYS:
                aged.append((age, fund["id"]))

        missing = perf.get("discreteMissingYears")
        if missing:
            behind.append((missing, fund["id"]))

    out: list[str] = []
    if aged:
        aged.sort(reverse=True)
        worst = ", ".join(f"{fid} ({age}d)" for age, fid in aged[:3])
        more = f", and {len(aged) - 3} more" if len(aged) > 3 else ""
        out.append(
            f"{len(aged)} {plural(len(aged), 'fund has', 'funds have')} "
            f"trailing cumulative figures older than {RESEARCH_STALE_DAYS} "
            f"days: {worst}{more}. Only the sector and benchmark comparators "
            f"need a person - the fund's own 1/3/5yr totals are computed from "
            f"its NAV series every run, and where the desk tracks four or more "
            f"funds in the same IA sector the card also carries a peer "
            f"comparison computed over the same windows.")
    if behind:
        behind.sort(reverse=True)
        worst = ", ".join(f"{fid} ({n})" for n, fid in behind[:3])
        more = f", and {len(behind) - 3} more" if len(behind) > 3 else ""
        out.append(
            f"{len(behind)} {plural(len(behind), 'fund is', 'funds are')} "
            f"missing a completed discrete year: {worst}{more}. The period "
            f"closed and was never added.")
    # Reported, not skipped. A table stating no period it was measured over
    # cannot be aged at all, and quietly omitting it from a staleness count
    # reads as a clean bill of health it has not earned.
    if unknown:
        named = ", ".join(unknown[:3])
        more = f", and {len(unknown) - 3} more" if len(unknown) > 3 else ""
        out.append(
            f"{len(unknown)} {plural(len(unknown), 'fund has', 'funds have')} "
            f"a cumulative table stating no measurement date, so "
            f"{plural(len(unknown), 'its', 'their')} age is unknown: "
            f"{named}{more}. Date the periods when re-researching.")
    return out


def dealing_health(doc: dict) -> list[str]:
    """Warn when a fund can no longer be bought through HL.

    This desk exists to inform purchases made through one platform. A fund
    that HL has closed to new investment, or suspended dealing in, is still a
    perfectly good factsheet and a completely useless suggestion - and nothing
    on the card would otherwise say so, because every other field goes on
    refreshing exactly as before.

    hl_factsheet.py reads the status off the page it is already fetching, so
    this costs no extra request. Funds with no reading yet are not warned
    about: silence here means not-yet-checked, not confirmed-tradeable, and
    saying otherwise would be the same trick the Shortlist badge used to play.
    """
    blocked, unchecked = [], 0
    for fund in doc["funds"]:
        d = fund.get("dealing")
        if not d:
            unchecked += 1
        elif not d.get("tradeable"):
            why = ", ".join(d.get("blocked") or []) or "no dealing prices on the factsheet"
            blocked.append(f"{fund['id']} ({why})")
    out = []
    if blocked:
        out.append(f"{len(blocked)} {plural(len(blocked), 'fund is', 'funds are')} "
                   f"not dealable through HL: {', '.join(blocked[:3])}"
                   f"{f', and {len(blocked) - 3} more' if len(blocked) > 3 else ''}. "
                   f"This desk only covers what can be bought here - drop them or "
                   f"confirm HL has reopened them.")
    if unchecked:
        out.append(f"{unchecked} {plural(unchecked, 'fund has', 'funds have')} no "
                   f"HL dealing check yet - run hl_factsheet.py to record one.")
    return out


def refresh_catalysts(doc: dict, today: date) -> dict:
    stats = {"refreshed": 0, "confirmed": 0, "provisional": 0,
             "estimated": 0, "failed": 0}
    resolved: dict[str, tuple[str, str, str | None]] = {}

    holdings = {c.get("holding")
                for f in doc["funds"] for c in (f.get("catalysts") or [])
                if c.get("holding")}

    for holding in sorted(holdings):
        result = cal.next_event(holding, today)
        if result:
            resolved[holding] = result
            # A provisional date is the publisher's own hedge - the Bank
            # marks next year's MPC dates that way - and is neither confirmed
            # nor estimated. Folding it into either would overstate one.
            if "(confirmed)" in result[0]:
                key = "confirmed"
            elif "(provisional)" in result[0]:
                key = "provisional"
            else:
                key = "estimated"
            stats[key] += 1
        else:
            stats["failed"] += 1

    for fund in doc["funds"]:
        for c in fund.get("catalysts") or []:
            hit = resolved.get(c.get("holding"))
            if not hit:
                continue
            c["date"], c["dateSource"], iso = hit
            c["refreshedAt"] = stamp(today)
            # dateISO is the sort anchor for the page's catalyst panel. The
            # displayed date stays fuzzy where the event is; this is the only
            # field that can be ordered or compared against today. Clear it
            # rather than leave a previous run's anchor attached to a date
            # string that has since been re-resolved to something else.
            if iso:
                c["dateISO"] = iso
            else:
                c.pop("dateISO", None)
            stats["refreshed"] += 1

    return stats


def write_report(stats, cat_stats, quotes, proxy_quotes, entries, warnings,
                 today, window_start) -> None:
    lines = [
        f"# Fund tracker - automated run {today.isoformat()}",
        "",
        f"Window: {stamp(window_start)} to {stamp(today)}",
        "",
        f"- **{stats['nav']}** funds priced from their own published NAV",
        f"- **{stats['computed']}** tracker funds priced from GBP ETF proxies "
        f"(no NAV series resolved for them)",
        f"- **{stats['unverified']}** funds marked not-yet-verified "
        f"(by design - nothing free publishes a figure for them)",
        f"- **{cat_stats['refreshed']}** catalyst dates refreshed "
        f"({cat_stats['confirmed']} confirmed, "
        f"{cat_stats['provisional']} provisional, "
        f"{cat_stats['estimated']} estimated)",
        "",
    ]
    if stats["nav_stale"]:
        # Sits with the other pricing counts, above the catalyst line.
        lines.insert(-2, f"- **{stats['nav_stale']}** stored NAV figures were "
                         f"older than {NAV_MAX_AGE_DAYS} days and were not "
                         f"used - those funds fell back to a proxy or to "
                         f"not-yet-verified")
    if warnings:
        lines += ["## Maintenance needed", ""]
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    if quotes:
        lines += ["## Market context", "", "| Index | Change |", "|---|---|"]
        lines += [f"| {lbl} | {q.format_pct()} |" for lbl, q in quotes.items()]
        lines.append("")
    nav_rows = sorted((fid, e) for fid, e in entries.items()
                      if e.get("basis", "").startswith("nav:"))
    if nav_rows:
        lines += ["## Fund NAV figures (1 month)", "",
                  "| Fund | Symbol | Change | Priced |", "|---|---|---|---|"]
        lines += [f"| {fid} | {e['basis'].split(':', 1)[1]} | {e['value']} "
                  f"| {e['asAt']} |" for fid, e in nav_rows]
        lines.append("")

    used = {fid: q for fid, q in proxy_quotes.items()
            if (entries.get(fid) or {}).get("basis", "").startswith("proxy:")}
    if used:
        lines += ["## Computed tracker figures (ETF proxy)", "",
                  "| Fund | Proxy | Change |", "|---|---|---|"]
        lines += [f"| {fid} | {q.symbol} | {q.format_pct()} |"
                  for fid, q in sorted(used.items())]
        lines.append("")
    lines += [
        "---",
        "",
        "*Automated. NAV figures are computed from each fund's own published "
        "NAV series; proxy figures come from an ETF tracking the same index "
        "and are not the fund itself - check the live factsheet before acting. "
        "Hand-entered discrete and cumulative factsheet tables are NOT "
        "refreshed by this run and continue to age. Not investment advice.*",
    ]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    args = parser.parse_args()

    today = date.today()
    window_start = today - timedelta(days=args.days)
    doc = json.loads(FUNDS.read_text(encoding="utf-8"))

    # Re-derive every fund's perfAsAt from its own period labels before
    # anything reads it. Done daily rather than only in the weekly factsheet
    # run so that a stamp written by the old behaviour - the read date, not the
    # measurement date - is corrected on the next run instead of waiting for
    # Sunday, and so a hand-edited table is dated by what it says the moment it
    # lands.
    restamped = sum(perf_dates.stamp(f, today) for f in doc["funds"])
    perfs = [f.get("performance") or {} for f in doc["funds"]]
    trailing = sum(1 for p in perfs if p.get("perfAsAt"))
    annual = sum(1 for p in perfs if p.get("discreteAsAt"))
    behind = sum(1 for p in perfs if p.get("discreteMissingYears"))
    print(f"Fund desk update - {today} (window {args.days}d)")
    print(f"[tables] {trailing} trailing tables dated, {annual} annual tables "
          f"dated ({behind} missing a closed year); {restamped} funds "
          f"restamped this run\n")
    print("Market context:")
    quotes = md.fetch_many(CONTEXT_TICKERS, window_start, today)

    print("\nTracker proxies:")
    proxy_quotes: dict[str, md.Quote] = {}
    for fid, proxy in FUND_PROXIES.items():
        q = md.fetch(proxy.ticker, window_start, today)
        if q:
            proxy_quotes[fid] = q
            print(f"  [data] {fid:40} {q.format_pct():>8}  ({q.source})")
        else:
            print(f"  [data] {fid:40} {'FAILED':>8}")

    # If nothing priced at all, the network is down or both providers changed.
    # Writing 63 blanks would silently destroy last week's good data, so abort.
    if not quotes and not proxy_quotes:
        print("\n[FAIL] No market data from any source. Leaving funds.json "
              "untouched rather than overwriting good data with blanks.",
              file=sys.stderr)
        return 1

    entries, stats = build_one_month(doc, quotes, proxy_quotes,
                                     today, window_start)
    print(f"\n[oneMonth] {stats['nav']} from own NAV, "
          f"{stats['computed']} from ETF proxies, "
          f"{stats['unverified']} unverified "
          f"({stats['proxy_failed']} proxy failures, "
          f"{stats['nav_stale']} NAV figures too stale to use)")

    cat_stats = refresh_catalysts(doc, today)
    print(f"[catalysts] {cat_stats['refreshed']} entries updated - "
          f"{cat_stats['confirmed']} confirmed, "
          f"{cat_stats['provisional']} provisional, "
          f"{cat_stats['estimated']} estimated, "
          f"{cat_stats['failed']} unresolved")

    # Two separate health checks, reported together but audited apart: one
    # watches the hardcoded central bank tables, the other the researched
    # factsheet data. Both need a person; neither can be fixed by this run.
    cal_warnings = cal.calendar_health(today)
    res_warnings = research_health(doc, today)
    deal_warnings = dealing_health(doc)
    warnings = cal_warnings + res_warnings + deal_warnings
    for w in warnings:
        print(f"[warn] {w}")

    if args.dry_run:
        print("\n[dry-run] nothing written")
        return 0

    for fund in doc["funds"]:
        fund["oneMonth"] = entries[fund["id"]]

    audit = doc.setdefault("auditLog", {"findings": []})
    audit["runDate"] = stamp(today)
    audit["summary"] = (
        f"{stamp(today)} automated run (deterministic, no LLM). Priced "
        f"{stats['nav']} funds from their own published NAV series and "
        f"{stats['computed']} tracker funds from GBP ETF proxies; "
        f"{stats['unverified']} funds marked 'not yet verified' with "
        f"market context attached - no fund-level figure was estimated for "
        f"them. Refreshed {cat_stats['refreshed']} catalyst entries "
        f"({cat_stats['confirmed']} confirmed dates, "
        f"{cat_stats['provisional']} provisional, "
        f"{cat_stats['estimated']} pattern estimates). "
        f"1yr/3yr/5yr figures are computed from each "
        f"fund's own published NAV series where one could be resolved; "
        f"discrete arrays and ISINs are untouched and continue to age."
        + (" CALENDAR WARNING: " + " ".join(cal_warnings)
           if cal_warnings else "")
        + (" RESEARCH STALE: " + " ".join(res_warnings)
           if res_warnings else "")
    )
    audit.setdefault("findings", []).append({
        "fundId": "ALL",
        "fundName": "Automated daily run",
        "check": "oneMonth-fill-deterministic",
        "severity": "INFO",
        "description": (
            f"Priced {stats['nav']} funds from their own NAV series and "
            f"{stats['computed']} tracker figures from GBP ETF proxies over a "
            f"{args.days}-day window; {stats['unverified']} funds left "
            f"unverified by design. "
            f"{cat_stats['refreshed']} catalyst dates refreshed."),
        "status": "corrected",
        "source": "Stooq / Yahoo Finance public endpoints; BoE and Fed "
                  "published calendars",
    })
    if cal_warnings:
        audit["findings"].append({
            "fundId": "ALL",
            "fundName": "Calendar maintenance",
            "check": "CALENDAR-LOW",
            "severity": "MEDIUM",
            "description": " ".join(cal_warnings),
            "status": "flagged",
            "source": None,
        })
    if res_warnings:
        audit["findings"].append({
            "fundId": "ALL",
            "fundName": "Research maintenance",
            "check": "RESEARCH-STALE",
            "severity": "MEDIUM",
            "description": " ".join(res_warnings),
            "status": "flagged",
            "source": None,
        })

    # The findings list is an append-only trail and the page no longer renders
    # it, so cap it: a daily run would otherwise add ~700 entries a year of
    # history nobody reads.
    audit["findings"] = audit["findings"][-AUDIT_TRAIL_MAX:]

    navs = sum(1 for f in doc["funds"]
               if (f.get("performance") or {}).get("nav1yr"))
    doc["meta"]["asAt"] = (f"{stamp(today)} (automated deterministic run); "
                           f"{navs} of {len(doc['funds'])} funds priced from "
                           f"their own NAV")
    doc["meta"]["built"] = f"{stamp(today)} (automated daily run)"
    # Regenerated every run so the page's caveats describe this run's data
    # rather than whatever was true when someone last edited them by hand.
    doc["meta"]["caveats"] = build_caveats(doc, entries, stats, today)

    FUNDS.write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                     encoding="utf-8")
    write_report(stats, cat_stats, quotes, proxy_quotes, entries, warnings,
                 today, window_start)
    print("\n[done] funds.json updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
