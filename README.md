<h1 align="center">Fund Tracker</h1>

<p align="center">
  <b><a href="https://jikkles.github.io/fund-tracker/">View the live tracker →</a></b>
</p>

<p align="center">
  <img alt="API keys: none" src="https://img.shields.io/badge/API_keys-none-2ea44f">
  <img alt="Cost: zero" src="https://img.shields.io/badge/cost-%C2%A30-2ea44f">
  <img alt="Python: stdlib only" src="https://img.shields.io/badge/python-stdlib_only-3776ab">
  <img alt="Hosting: GitHub Pages" src="https://img.shields.io/badge/hosting-GitHub_Pages-24292e">
</p>

An automated research desk covering **103 funds** across sixteen categories. The core is the
Hargreaves Lansdown Wealth Shortlist, read from HL's own published list; around it sit funds
HL has dropped but the desk still tracks, and funds covering asset classes the Shortlist does
not include at all — property, infrastructure, commodities, index-linked gilts and cash.
Every fund prices from its own published NAV series. GitHub Actions price them all from free
market data, roll the catalyst calendar forward, and republish the page. Standard library
only, free public endpoints, inside GitHub's free Actions allowance.

> **Not investment advice.** Past performance is not a guide to future returns.
> Check the live factsheet before acting on anything here.

---

## What's on the page

| Panel | What it shows |
|---|---|
| **Index chart** | 12 indices (FTSE 100/250, S&P 500, Nasdaq, Dow, Euro STOXX 50, Nikkei, Hang Seng, gold, Brent, US 10yr, GBP/USD) across 1D · 5D · 1M · 6M · YTD · 1Y · 5Y, inline SVG with a crosshair readout |
| **Top 5 / Bottom 5** | Ranked over 1W · 1M · 1Y · 5Y from each fund's own NAV series. Only priced funds appear, and the header says how many |
| **Watchlist** | Top 8 to hold over six months to five years, scored on 1yr vs sector, OCF, max drawdown and badge signal. Capped at two per group, and a fund with no three-year record sits out rather than being ranked on one year and a run of blanks |
| **Fund cards** | Holdings, sector and country splits, size, charges, managers, discrete annual returns — scraped from HL factsheets |
| **Headlines** | Split by market, one tab per category, expanding in place |
| **Catalysts** | Central bank and earnings dates, each labelled `confirmed`, `provisional` or `estimated` |
| **Data health** | Live signals only — stale, inconsistent, unverified |

---

## The list, and what the badges mean

Sixteen categories, sized by what is actually out there rather than a fixed ten per bucket:

`UK Growth` · `UK Equity Income` · `UK Small & Mid` · `North America` · `Europe` · `Japan` ·
`Asia` · `Emerging Markets` · `Global` · `Property & Infrastructure` · `Specialist & Thematic` ·
`Corporate & Gilts` · `Strategic & Global Bonds` · `Cash & Index-Linked` · `Mixed Investment` ·
`Total Return`

The seven-by-ten grid this replaced was forcing Japan, China and emerging markets into one
row while ten near-identical Europe funds sat in another — the widest-dispersion bucket was
the most compressed, and the narrowest had the most slots.

| Badge | Meaning |
|---|---|
| **Wealth Shortlist** | On HL's list as at the last run |
| **Ex-Shortlist** | Dropped by HL, still tracked here |
| *(none)* | Never on it — the asset classes HL does not shortlist |

Status comes from `scripts/shortlist.py`, which reads HL's published Wealth Shortlist data
and matches on SEDOL. It is HL's view, not a rating by this desk.

> It used to be inferred from factsheet prose, by looking for "our analysts have selected
> this fund for the Wealth Shortlist". HL ship that sentence six times in a hidden tooltip
> template on *every* factsheet, so the test was true for every fund and 68 of 70 carried a
> badge that meant nothing. A list published as a list should be read as one.

---

## Setup — three steps, about five minutes

1. **Create a public repo** and push these files. Pages needs a public repo on the Free plan.
2. **Settings → Pages → Source: GitHub Actions.**
3. **Actions tab → "Daily fund update" → Run workflow.**

Watch it go green, then open `https://<you>.github.io/<repo>/`. There is no step 4 —
nothing to configure, no secrets to add.

---

## How it runs

| Workflow | Schedule | Does |
|---|---|---|
| `daily-update.yml` | Daily | Prices every fund, rolls catalysts, deploys the page |
| `hourly-market.yml` | Weekdays at 08:17, 13:17, 17:17 and 21:17 UTC | Refreshes the index chart and headlines |
| `weekly-factsheets.yml` | Weekly | Re-scrapes HL factsheet depth |

Weekly is deliberate for factsheets: holdings tables are republished monthly at best, and
they are someone else's pages — asking once a week rather than seven times is basic courtesy.

`data/market.json` is **not committed**. It is regenerated on every deploy and published
straight to the Pages artifact, so a chart refresh doesn't leave the repo carrying commits of
churning price data.

---

## How funds are priced

Every fund resolves to its own NAV series. Yahoo carries UK OEICs under Morningstar-style
symbols (`0P0000W36K.L`) in GBP, priced daily, so `fund_nav.py` computes real 1/3/5yr total
returns rather than serving a researched figure that ages.

**Resolution ladder** — strongest identifier first:

```
stored navSymbol  →  fund ISIN  →  ISIN found elsewhere in the record
   →  name + house abbreviation  →  FT search API (returns ISINs Yahoo can't find by name)
```

Resolution is the dangerous step: a wrong symbol yields confident, precise, *wrong*
performance data, which is worse than an honestly stale figure. So a match must clear
several bars:

- an ISIN hit must be corroborated by the fund name
- a name match must be a London-listed GBP **accumulation** line (income classes understate total return)
- no two funds may claim the same symbol
- the NAV series must contain no jump a real return cannot explain

That last rule caught two funds that had been through 100:1 share-class redenominations and
would otherwise have published −98.9% three-year returns.

**Tracker proxies are the fallback.** 17 tracker funds map to a London-listed GBP ETF
following the same index. Every fund currently resolves to its own NAV, so the proxy table
prices nothing — it earns its place for the run where a NAV lookup fails. Pricing a GBP ETF
also beats estimating from headline index moves: in a week where the S&P rises 2% and
sterling strengthens 2%, the raw index says +2% and a UK investor got roughly nothing.

---

## Where the rest comes from

**Factsheet depth.** `hl_factsheet.py` reads each fund's HL factsheet for holdings, splits,
size, launch date, yield, charges, managers and discrete annual periods. Two guards protect
it: the page title must match the fund name (this caught a scrape landing on HL's generic
"Wealth Shortlist Trackers" page), and share class is compared, because charges, yield and
discrete history are class-specific and get dropped when the classes differ. The date is
stamped on every successful read, not only on a change — a fund confirmed unchanged today
counts as verified, not stale.

**Central bank dates.** `cb_calendar.py` reads the published Fed and BoE calendars, which
carries the desk through Dec 2027 untouched. It fails closed: a year is accepted only if it
yields a plausible number of meetings (both committees meet eight times a year), unparsed
rows are dropped rather than guessed at, and dates outside a sane horizon are refused. The
hand-maintained table in `calendar_data.py` is a **floor, not a legacy** — where it and the
scrape disagree on a future date, the hand-entered one wins and the run says so.

**Catalyst ordering.** Every resolved catalyst carries `dateISO`, a machine-readable sort
anchor that is never displayed. The visible date stays honestly fuzzy (`~mid Oct 2026` for a
pattern estimate) while the anchor keeps the panel in date order.

**Headlines.** Yahoo's feed is per-symbol, so the panel is relevant by construction: no
editorial call about what "affects markets". Each market pools several symbols round-robin,
and repeated headline series are capped at two, or one publisher's recurring column crowds
out a whole market.

**The page loads itself.** `index.html` fetches `funds.json` with `cache: 'no-store'` and a
cache-busting query, so neither the browser nor GitHub's CDN can serve yesterday's copy. A
`localStorage` copy exists purely so a repeat visit paints instantly; the header chip says
`live data` or `offline copy` so you always know which you're looking at.

---

## Files

| File | Does |
|---|---|
| `scripts/run_update.py` | Orchestrates the run |
| `scripts/fund_nav.py` | Resolves each fund to a Yahoo NAV symbol and prices it — `--resolve-only` to check matches |
| `scripts/hl_factsheet.py` | Refreshes depth from HL factsheets — `--dry-run`, `--only <id>`, `--new`, `--repair-text` |
| `scripts/market_series.py` | Index chart series + per-index headlines — **edit `INDICES`** to change the tabs |
| `scripts/market_data.py` | Stooq primary, Yahoo fallback, per-symbol failure |
| `scripts/cb_calendar.py` | Reads the published Fed/BoE calendars |
| `scripts/calendar_data.py` | Central bank dates + earnings lookup |
| `scripts/proxies.py` | Fund → ETF mapping. **Edit this** if a proxy looks wrong |
| `scripts/anonymise.py` | Strips personal references before publishing |

### Check it locally — writes nothing

```bash
python scripts/market_data.py    --selftest   # endpoints reachable?
python scripts/market_series.py  --selftest   # same, for the chart endpoint
python scripts/cb_calendar.py    --selftest   # Fed/BoE calendar parsing, offline
python scripts/calendar_data.py  --selftest   # date rollover + sort anchors, offline
python scripts/fund_nav.py       --selftest   # resolution guards, offline
python scripts/hl_factsheet.py   --selftest   # scrape parsing + guards, offline

python scripts/cb_calendar.py                 # what the calendars say today
python scripts/run_update.py     --dry-run    # full run, writes nothing
python scripts/market_series.py  --dry-run    # chart fetch, writes nothing
```

---

## Known limitations

**Read this before trusting the output.**

#### Data sources

- **Both sources are unofficial.** Neither publishes an SLA for this use — that's the
  trade-off for zero cost and zero credentials. Failure is per-symbol: an unpriceable fund is
  reported as unverified rather than guessed, and if *nothing* prices, the run aborts rather
  than overwriting good data with blanks.
- **The live fetch paths were never exercised during development** — the sandbox couldn't
  reach Stooq or Yahoo. Parsing is unit-tested against captured payloads. **Run `--selftest` first.**
- **The chart is Yahoo-only.** Unlike the fund figures it has no Stooq fallback; Yahoo is the
  only free source here publishing intraday bars. An index that won't fetch is dropped from
  the tabs; if nothing fetches, the run fails and the previous deploy stays live.
- **The central bank calendar is scraped, so it can break.** It degrades to the hardcoded
  table rather than to nothing, and the run warns when the merged calendar is within 75 days
  of exhausting. If the scrape breaks *and* the table runs out, the rate chip says "next date
  not published" — top up `calendar_data.py`.

#### Coverage gaps

- **10 priced funds use a substitute share class.** Another *accumulation* class of the same
  fund, with an asterisk on the NAV chip naming the swap. Charges differ between classes, so
  the return does too. Income and distributing classes are never substituted.
- **1 fund has too little NAV history for a 1-year figure.** BlackRock Continental European
  Income prices fine, but Yahoo carries only about two months of its series, so its 1yr number
  is still the researched one and still ages. Data Health goes on flagging it as stale for
  exactly that reason.
- **An unmatched fund ages silently.** None are unmatched today, but the failure mode matters:
  the fund keeps its researched 1yr figure, which drives the Winning/Lagging panels. Storing a
  correct `isin` or `navSymbol` by hand fixes it — the resolver trusts a stored identifier.
- **Headline coverage is uneven.** `^FTMC` (FTSE 250) returns no feed at all, and the panel
  says so rather than borrowing another index's news. The FTSE 100 feed routinely carries
  week-old items, so every headline is stamped with its age. News never fails the run.
- **Proxies are close, not exact.** A FTSE All-Share fund priced off a FTSE 100 ETF misses
  mid-cap divergence; ESG tilts diverge more. Every figure carries `verify vs HL factsheet`.

#### Fine print

<details>
<summary><b>Share-class edge cases, HL mojibake, name-match limits, cron drift</b></summary>

<br>

- **A hand-pinned HL link is trusted on identity.** The 60% name-overlap floor stands in for a
  person confirming the page, so where a person already has, it adds nothing — and it was
  actively harmful on L&G Future World, where HL's "Future Wrld ESG Tilted & Opt UK Id" scores
  57% and locked out a correct factsheet. Set `links.hl` and `hlSource: "manual"` to pin one.
  The share-class check is *not* skipped for a pinned link.
- **`hlClassConfirmed: false` forces the conservative reading.** The letter comparison stays
  silent when our own `shareClass` carries no letter, which isn't the same as the classes
  matching. Holdings, sectors, countries, size and manager are taken; charges, yield and
  discrete history are not.
- **The name-overlap floor is weaker than it looks.** `NAME_MATCH_MIN` does not separate
  "Artemis Income" from "Artemis Global Income" — "income" is a stopword, so the pair scores a
  perfect 1.0. What actually keeps such funds apart is the ISIN rung and `reject_collisions`;
  the floor only screens out the plainly unrelated. `fund_nav.py --selftest` pins this.
- **HL publish mojibake, and the desk repairs it.** Their factsheets carry the literal entities
  for `4¼%` mangled through cp1252. `text_of` puts it back through the encoding that mangled it
  and only accepts the result when it re-mangles to exactly the input, so text that was never
  mangled is left alone. If HL's mangling changes shape the guard refuses it and the raw text
  shows through — the safe direction.
- **Discrete history carrying sector comparators is never overwritten.** HL publishes fund
  returns without comparators, over periods ending in August where the researched ones end in
  March, so copying one across would attach it to a period it was never measured over.
- **GitHub's cron drifts** by an hour or more, and scheduled workflows auto-disable after 60
  days of repo inactivity. The chart asks for four fires a day rather than sixteen, at :17
  rather than the busiest on-the-hour slot, because asking for less gets more: over the six
  days to 31 Aug 2026, roughly 80 requested hourly fires produced 11 actual runs. Treat the
  schedule as best-effort - a dropped run means a stale chart, not a wrong one.
- **The audit trail is capped at 30 entries** and is no longer rendered on the page. It
  survives in the JSON as a machine-readable record; a daily append would add ~700 entries a
  year.
- **Everything here is public**, including the audit log. `anonymise.py` runs as a build step
  and *fails the build* rather than publishing a personal reference or internal-process
  vocabulary. Those are refused rather than rewritten — the sentence usually needs rethinking,
  not a word swapped.

</details>
