# Fund Tracker

An automated research desk covering 70 funds - ten in each of seven
categories - drawn from the Hargreaves Lansdown Wealth Shortlist, with
additions where the Shortlist does not carry ten in a category. A daily GitHub
Action prices every fund from free market data, rolls the catalyst calendar
forward, and republishes the page.

Ten per category is a floor, not decoration. The Winning and Lagging columns
take the top three and bottom three of one sorted list, so a category holding
four priced funds showed the same fund in both columns.

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

**Tracker funds get real numbers.** The proxy table maps 17 tracker funds to a
London-listed, GBP-denominated ETF following the same index, and prices a
30-day window from it. This is now a fallback: every fund on the list resolves
to its own NAV series, so the last several runs have priced 0 funds from a
proxy. It earns its place for the run where a NAV lookup fails.

This is deliberately better than estimating from headline index moves. A UK
investor in a US tracker earns the index move *plus* the USD/GBP move — in a
week where the S&P rises 2% and sterling strengthens 2%, the raw index says
+2% and the investor got roughly nothing. Pricing a GBP ETF captures that.

**Funds are priced from their own NAVs.** The desk originally assumed no free
source published active fund NAVs. That was wrong: Yahoo carries UK OEICs under
Morningstar-style symbols (`0P0000W36K.L`), in GBP, priced daily. So
`fund_nav.py` resolves each fund to such a symbol and computes real 1/3/5yr
total returns. 67 of 70 funds now carry a figure priced today rather than a
researched one that ages, and the other three - share classes younger than a
year - carry real 1-week and 1-month figures instead of a back-filled guess.

Resolution is a ladder, strongest identifier first: a stored `navSymbol`, then
the fund's ISIN, then any ISIN found elsewhere in its record, then the name and
its house abbreviation (Yahoo lists "L&G UK Index" and will not match a search
for "Legal & General UK Index"), and finally **FT's search API**, which returns
ISINs for funds Yahoo cannot find by name at all. An ISIN is an identifier; a
name search is a lottery.

Nothing is guessed. Resolution is the dangerous step — a wrong symbol yields
confident, precise, wrong performance data, which is worse than an honestly
stale figure — so a match must clear several bars: an ISIN hit must be
corroborated by the fund name, a name match must be a
London-listed GBP **accumulation** line (income classes understate total
return), no two funds may claim the same symbol, and the NAV series must
contain no jump that a real return cannot explain. Two funds were caught by
that last rule alone: Invesco Tactical Bond and Ninety One Diversified Income
had both been through 100:1 share-class redenominations, which would otherwise
have published as −98.9% three-year returns.

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

**The Top 5 / Bottom 5 lists sort by period.** 1W, 1M, 1Y or 5Y, each computed
from the fund's own NAV series, defaulting to 1W. Only funds that could be
priced appear, and the header says how many, so a shrinking count is visible
rather than silent. "vs sector" is offered on 1Y alone: the sector averages
are researched figures on a different basis, and comparing a NAV return
against one would not be like-for-like.

**Headlines are split by market.** UK, US, Europe, Asia/EM/Japan, Global,
Bonds and Multi-Asset — the same groups the funds use. Yahoo's headline feed
is per-symbol, which is what makes the panel relevant *by construction*: there
is no editorial call about what "affects markets", and no model deciding. Each
market pools several related symbols, round-robin rather than by date, because
gold simply has a busier feed than Brent and would otherwise fill Multi-Asset
on its own. Repeated headline series are capped at two — publishers run the
same column every session ("European Stocks Close ... in Monday Trading"), and
without the cap one series crowds out a whole market. It refreshes hourly with
the chart.

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

**Factsheet depth is scraped, not typed.** `hl_factsheet.py` reads each fund's
HL factsheet and refreshes the parts a NAV series cannot supply: top ten
holdings, sector and country splits, fund size, launch date, historic yield,
the net ongoing charge and HL's saving, manager names with start dates, and
five discrete annual periods. That closes the last hand-maintained surface on
the desk - the 120-day stale-research warning used to name eight funds and now
names none.

The same rule applies here as everywhere else: nothing is guessed. A field is
written only when it is found and parses, and a page is used only after it is
confirmed to be this fund. Two guards do that work. The first compares the
page's own title against the fund name and rejects a mismatch - which is how a
scrape of two trackers was caught landing on HL's generic "Wealth Shortlist
Trackers" page instead of a factsheet. The second compares share class, because
HL frequently lists a different retail class than the desk prices: charges,
yield and discrete history are class-specific and are dropped when the classes
differ, while holdings, fund size and manager - which describe the whole fund -
are still taken. Six funds are in that state today and say so.

It also refuses to overwrite a discrete history that already carries sector
comparators. HL publishes fund returns on that table but no comparator, and its
periods end in August where the researched ones generally end in March, so
copying a comparator across would attach it to a period it was never measured
over.

**Catalysts roll forward automatically, and now in the right order.** Central
bank dates are read from the Fed and BoE published calendars and marked
`(confirmed)` or `(provisional)` as the publisher marks them. Earnings dates
are fetched live where a company has confirmed one, and fall back to a pattern
estimate marked `(estimated)` where it has not. Never the other way round.

Every resolved catalyst also carries `dateISO`, a machine-readable sort
anchor. The displayed date is deliberately fuzzy where the event is - `~mid
Oct 2026` is the honest form of a pattern estimate - but a fuzzy string cannot
be ordered or compared against today, which is why the panel used to publish
in raw array order with events that had already happened still in it. The
anchor is never displayed; for an estimate it is the middle of the stated
third of the month, an ordering device rather than a claim of precision. The
page parses the date text as a fallback, so a hand-entered catalyst the
resolver does not cover still sorts rather than sinking to the bottom.

**The central bank dates are read from the published calendars.** They used
to be typed into `calendar_data.py` by hand, which worked until it did not:
the FOMC table was down to a single date, the run had been warning about it
for weeks, and the table was *already wrong* - it named 28 Oct as the next
Fed decision when the published calendar has one on 16 Sep. `cb_calendar.py`
now reads both pages, which carries the desk through Dec 2027 without a
person touching it.

The scrape is the dangerous step, so it fails closed. A year is accepted only
if it yields a plausible number of meetings - both committees meet eight times
a year, so a panel parsing to two means the markup moved, not that six
meetings were cancelled. Rows it does not understand are dropped rather than
guessed at; the Fed lists the occasional "22 (notation vote)" among the
scheduled meetings and a notation vote is not a policy decision. Dates outside
a sane horizon are refused, which catches a parse locked onto the wrong number
entirely. And the hand-maintained table is kept as a **floor, not a legacy**:
where the scrape and a hand-entered future date disagree, the hand-entered one
wins and the run says so, because a hand-checked date is worth more than a
regex and a silent overwrite is how a bad parse would reach the page. Beyond
the table's horizon the scrape stands alone, which is the entire point.

**A provisional date is not a confirmed date.** The Bank publishes one year
under "confirmed dates" and the next under "provisional dates" - that is the
page's own word, not an inference - and it is carried through to the label and
counted in its own bucket in the run report. Relabelling a provisional date as
confirmed would be the same failure as dressing a pattern estimate up as a
fetched one, which is what this module exists to prevent.

Two events had no company calendar to fetch and were written out as fixed
strings naming 2026. Those now roll from an annual rhythm like any other
estimate: a hardcoded `~Nov 2026` would have gone on saying `~Nov 2026`
throughout 2027, which is a stale date wearing a forward-looking label - the
same failure the confirmed/estimated split exists to prevent, arrived at from
the other direction. TotalEnergies also stopped being an exception: its date
comes from the normal ladder now and its "oil exposure is continuous rather
than event-driven" caveat is appended to it, rather than replacing it.

---

## Files

| File | Does |
|---|---|
| `scripts/run_update.py` | Orchestrates the run |
| `scripts/proxies.py` | Fund → ETF mapping. **Edit this** if a proxy looks wrong |
| `scripts/market_data.py` | Stooq primary, Yahoo fallback, per-symbol failure |
| `scripts/calendar_data.py` | Central bank dates + earnings lookup |
| `scripts/cb_calendar.py` | Reads the published Fed/BoE calendars. `--selftest` |
| `scripts/market_series.py` | Index chart series + per-index headlines. **Edit `INDICES`** to change the tabs |
| `scripts/fund_nav.py` | Resolves each fund to a Yahoo NAV symbol and prices it. `--resolve-only` to check matches |
| `scripts/hl_factsheet.py` | Refreshes researched depth from HL factsheets. `--dry-run`, `--only <id>`, `--new` |
| `scripts/anonymise.py` | Strips personal references before publishing |

Test locally without writing anything:

```bash
python scripts/market_data.py --selftest    # check the endpoints are reachable
python scripts/market_series.py --selftest  # same, for the chart endpoint
python scripts/cb_calendar.py --selftest    # Fed/BoE calendar parsing, offline
python scripts/calendar_data.py --selftest  # date rollover + sort anchors, offline
python scripts/cb_calendar.py               # what the published calendars say today
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

- **10 priced funds use a substitute share class.** Where the exact class is
  not listed, another *accumulation* class of the same fund is used and the
  NAV chip carries an asterisk naming the swap. Charges differ between
  classes, so the return does too. Income and distributing classes are never
  substituted — they pay dividends away and would understate total return.

- **1 fund has too little NAV history for a 1-year figure.** BlackRock
  Continental European Income resolves and prices, but Yahoo only carries
  about two months of its series, so it contributes to the 1W and 1M
  rankings and its own rolling 1-month figure while its **1-year number is
  still the researched one, and still ages**. The Data Health strip goes on
  reporting it as stale for exactly that reason — a fresh price date on the
  short windows is deliberately not allowed to vouch for the 1yr figure.

- **A fund the resolver cannot match keeps ageing silently.** None are
  unmatched today, but the failure mode is worth knowing: the fund keeps its
  researched 1-year figure, which drives the Winning/Lagging panels and the
  headline number on its card. Three funds sat in that state until their
  ISINs were confirmed and stored — one of them, Man GLG Continental European
  Growth, is registered without the "GLG", so no name search was ever going
  to find it. Storing a correct `isin` or `navSymbol` by hand is enough to
  bring such a fund in: the resolver trusts a stored identifier.

- **The central bank calendar is scraped, so it can break.** It is read from
  the Fed and BoE pages, and a page can be redesigned. Failure degrades to the
  hardcoded table rather than to nothing, and the run still warns when the
  merged calendar is within 75 days of exhausting - but if both the scrape
  breaks *and* the table runs out, the rate chip says "next date not
  published" instead of naming a date. Top up `calendar_data.py` if that
  happens.

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
