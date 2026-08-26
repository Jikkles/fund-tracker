# Fund Tracker

An automated research desk covering the ~63 funds on the Hargreaves Lansdown
Wealth Shortlist. A weekly GitHub Action prices the index trackers from free
market data, rolls the catalyst calendar forward, and republishes the page.

**No API key. No account. No cost.** Standard library only, free public
endpoints, runs entirely inside GitHub's free Actions allowance.

**Not investment advice.** Past performance is not a guide to future returns.
Check the live factsheet before acting on anything here.

---

## Setup — three steps, about five minutes

1. **Create a public repo** and push these files.
   Pages needs a public repo on the Free plan.

2. **Settings → Pages → Source: GitHub Actions.**

3. **Actions tab → "Weekly fund update" → Run workflow.**
   Watch it go green, then check `https://<you>.github.io/<repo>/`.

There is no step 4. Nothing to configure, no secrets to add.

---

## What it actually does

**Tracker funds get real numbers.** Each of the 17 index trackers is mapped to
a London-listed, GBP-denominated ETF following the same index. The script
prices a 30-day window and computes the actual change.

This is deliberately better than estimating from headline index moves. A UK
investor in a US tracker earns the index move *plus* the USD/GBP move — in a
week where the S&P rises 2% and sterling strengthens 2%, the raw index says
+2% and the investor got roughly nothing. Pricing a GBP ETF captures that.

**Active funds get an honest gap.** No free source publishes active fund NAVs,
so they are marked `not yet verified` with the real index moves for their
market attached as context. The note says explicitly that this is context, not
the fund's return.

The desk stays honest *by construction* — there is no prompt to disregard and
no model to drift.

**Catalysts roll forward automatically.** Central bank dates are hardcoded from
published calendars and marked `(confirmed)`. Earnings dates are fetched live
where a company has confirmed one, and fall back to a pattern estimate marked
`(estimated)` where it has not. Never the other way round.

---

## Files

| File | Does |
|---|---|
| `scripts/run_update.py` | Orchestrates the run |
| `scripts/proxies.py` | Fund → ETF mapping. **Edit this** if a proxy looks wrong |
| `scripts/market_data.py` | Stooq primary, Yahoo fallback, per-symbol failure |
| `scripts/calendar_data.py` | Central bank dates + earnings lookup |
| `scripts/anonymise.py` | Strips personal references before publishing |

Test locally without writing anything:

```bash
python scripts/market_data.py --selftest    # check the endpoints are reachable
python scripts/run_update.py --dry-run      # full run, writes nothing
```

---

## Known limitations

**Read this before trusting the output.**

- **The live fetch paths were never exercised during development** — the
  sandbox they were written in could not reach Stooq or Yahoo. Parsing logic is
  unit-tested against captured payloads. **Run `--selftest` first.**

- **Both data sources are unofficial.** Neither publishes an SLA for this use.
  That is the trade-off for £0 and zero credentials. Failure is handled
  per-symbol: a fund whose proxy cannot be priced is reported as unverified
  rather than guessed, and if *nothing* prices the run aborts rather than
  overwriting good data with blanks.

- **Proxies are close, not exact.** A FTSE All-Share fund priced off a FTSE 100
  ETF misses mid-cap divergence. ESG-tilted funds will diverge more. Each note
  states its own caveat and every figure carries `verify vs HL factsheet`.

- **1-year figures are never refreshed.** Those drive the Winning/Lagging
  panels and the headline number on every card, and they go stale silently. As
  of writing they were ~45 days old. The automation will not fix this and will
  keep saying so in the audit log — run a manual verification sweep
  periodically.

- **The hardcoded central bank calendar runs out.** It currently covers to
  Dec 2026 (BoE) and Oct 2026 (FOMC). The run warns when it is within 75 days
  of exhausting and logs a `CALENDAR-LOW` finding. Top up `calendar_data.py`
  from the published calendars when it does.

- **The catalyst panel does not filter by date.** `site/index.html` builds the
  Upcoming Catalysts column in raw array order with no date filter and no
  chronological sort (~line 1042, `.slice(0,6)`). Fresh data still ages into a
  stale-looking panel between runs. ~5 lines to fix; not yet applied.

- **GitHub's cron drifts** by up to an hour or more, and scheduled workflows
  are auto-disabled after 60 days of repo inactivity.

- **Everything here is public**, including the audit log. `anonymise.py` runs
  as a build step and fails the build rather than publishing a personal
  reference.
