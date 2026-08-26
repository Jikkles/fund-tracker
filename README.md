# Fund Tracker

An automated research desk covering the ~63 funds on the Hargreaves Lansdown
Wealth Shortlist. A daily GitHub Action prices the index trackers from free
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

3. **Actions tab → "Daily fund update" → Run workflow.**
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

**There is an index chart at the top.** Twelve indices (FTSE 100 and 250,
S&P 500, Nasdaq, Dow, Euro STOXX 50, Nikkei, Hang Seng, gold, Brent, the US
10yr and GBP/USD) with 1D / 5D / 1M / 6M / YTD / 1Y / 5Y ranges, drawn as
inline SVG with a crosshair readout. A second workflow refreshes it roughly
hourly on weekdays, 07:17-22:17 UTC, which covers the LSE open through the US
close.

Four series are fetched per index and the rest are derived in the browser:
5-minute bars for 1D, 30-minute for 5D, daily for 1Y, weekly for 5Y. 1M, 6M
and YTD are sliced out of the 1Y series rather than fetched, which is why the
whole panel costs four requests per index instead of seven and lands at about
150 KB. x is positioned by bar index rather than by timestamp, so overnight
and weekend gaps do not open dead space in the line - the same thing a quote
page does.

`data/market.json` is **not committed**. It is regenerated on every deploy and
published straight to the Pages artifact, so an hourly refresh does not leave
the repo carrying two dozen commits a day of churning price data.

**Headlines follow the chart.** The panel under the fund tables shows news
for whichever index tab is selected — click Nikkei, get Nikkei news. Yahoo's
headline feed is per-symbol, which is what makes the panel relevant *by
construction*: there is no editorial call about what "affects markets", and no
model deciding. It refreshes hourly with the chart.

Rows expand in place to show the feed's own summary rather than navigating
away; the **Full story** link on the right is the only part that leaves the
page. Feed descriptions arrive truncated mid-sentence, so the ellipsis is
normalised and the cut is visible rather than reading as a typo.

**The page loads itself.** `index.html` fetches `funds.json` from the same
folder on every visit, with `cache: 'no-store'` and a cache-busting query so
neither the browser nor GitHub's CDN can serve yesterday's copy. There is no
file picker and no drag-and-drop — open the URL and it is current. A copy is
kept in `localStorage` purely so a repeat visit paints instantly and still
shows something offline; the fetched file replaces it unconditionally the
moment it arrives, and the header chip says `live data` or `offline copy` so
you always know which one you are looking at.

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
| `scripts/market_series.py` | Index chart series + per-index headlines. **Edit `INDICES`** to change the tabs |
| `scripts/anonymise.py` | Strips personal references before publishing |

Test locally without writing anything:

```bash
python scripts/market_data.py --selftest    # check the endpoints are reachable
python scripts/market_series.py --selftest  # same, for the chart endpoint
python scripts/run_update.py --dry-run      # full run, writes nothing
python scripts/market_series.py --dry-run   # chart fetch, writes nothing
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

- **The audit trail is capped at the last 30 entries.** It is no longer
  rendered on the page — the Data Health strip now shows only live signals
  (stale, inconsistent, unverified) rather than a tally of what past manual
  research runs corrected. The trail survives in the JSON as a machine-readable
  record, but a daily append would otherwise add ~700 entries a year.

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
  are auto-disabled after 60 days of repo inactivity. The hourly chart refresh
  is scheduled at :17 rather than on the hour, which is the busiest slot and
  drifts worst, but "hourly" still means roughly, not on the dot.

- **Headline coverage is uneven and can be stale.** Yahoo publishes no feed
  at all for some symbols — `^FTMC` (FTSE 250) returns nothing, and the panel
  says so rather than borrowing another index's news. Where a feed does exist
  it is not always busy: the FTSE 100 feed routinely carries items a week old.
  Every headline is stamped with its age so you can see that at a glance. News
  never fails the run; an unreachable feed degrades to no headlines.

- **The chart is a single unofficial source.** Unlike the fund figures, which
  fall back from Stooq to Yahoo, the chart is Yahoo-only - it is the only free
  source here publishing intraday bars, and 1D/5D are the point of the panel.
  An index that will not fetch is dropped from the tabs; if *nothing* fetches
  the run fails and the previous deploy stays live rather than being replaced
  by an empty chart.

- **Everything here is public**, including the audit log. `anonymise.py` runs
  as a build step and fails the build rather than publishing a personal
  reference. It also refuses internal-process vocabulary — a fund note once
  shipped reading "top of the manual work order — an HL screenshot or a
  Claude in Chrome run would resolve it", which is a note to the maintainer,
  not information about the fund. Those are refused rather than rewritten:
  the sentence usually needs rethinking, not a word swapped.
