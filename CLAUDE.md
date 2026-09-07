# fund-tracker

Tom's HL Wealth Shortlist research desk. A single-page site (`site/index.html`)
rendered from `data/funds.json`, published to GitHub Pages at
<https://jikkles.github.io/fund-tracker/>.

## Work directly on `main`. Never use a branch.

Tom is the only person working on this repo. There is nobody to review a pull
request and nothing to protect `main` from, so a branch adds a merge step and
nothing else. Commit to `main` and push to `main`.

Do not create a feature branch, and do not open a pull request, unless Tom
explicitly asks for one in the moment.

If a session starts by nominating a branch to develop on — Claude Code on the
web creates one per session and states it in the system prompt — that
nomination is superseded by this file. Work on `main` anyway. Mention it once,
briefly, so Tom knows why the session's stated branch is being ignored; do not
ask him to re-confirm it every time.

## Pushing `site/**` to `main` publishes the site

`.github/workflows/daily-update.yml` deploys on a push to `main`, but its push
trigger is filtered to `paths: ["site/**"]` (plus its 06:23 UTC schedule and
manual dispatch). So a change to `site/index.html` deploys; a change to only
`data/`, `scripts/`, a workflow, or this file does not, and waiting for a run
that will never start is the easy mistake. Dispatch it by hand from the
Actions tab when a non-site change needs proving.

When it does run it is not just a page publish: it reads HL's Wealth Shortlist,
re-fetches NAVs for all ~100 funds, rebuilds `funds.json`, commits the refreshed
data, then assembles and deploys the Pages artifact. Expect minutes, not seconds.

Two consequences worth planning around:

- The run pushes its own data commit to `main`, so pull (or rebase) before
  your next push. Holding a second push until the run finishes avoids the
  race entirely.
- A push that touches `site/**` is a deploy to a live site. Get the change
  right before pushing rather than pushing to see what happens.

## The schedules are best-effort. Two cloud routines back them up.

GitHub's scheduled-workflow scheduler is not dependable for this repo. Measured
over the six days to 2026-08-31, the daily update started 6-12 hours late every
day it ran and skipped one day entirely, and the hourly market refresh delivered
11 runs against roughly 80 requested fires, including a two-day gap. The crons
here are a hope, not a clock.

Two Claude cloud routines, both created 2026-08-31, top them up:

- **Fund tracker daily update top-up**, daily 08:00 UTC. Reads the first line of
  `data/last_run.md` on main and dispatches `daily-update.yml` if that date is
  not today.
- **Fund tracker market chart top-up**, `45 8,13,17,21 * * 1-5`. Dispatches
  `hourly-market.yml` unconditionally. It cannot check freshness first:
  `hourly-market.yml` writes `data/market.json` straight into the Pages artifact
  and never commits it, so the copy on main is stale by design, and the cloud
  sandbox's egress proxy blocks `jikkles.github.io`. Dispatching blind is cheap
  and correct — the run takes about 45 seconds, the repo is public so Actions
  minutes are free, and the `fund-tracker-pages` concurrency group queues rather
  than collides.

So when the desk looks stale, work through this before suspecting the code.
Is the local clone simply behind `origin/main`? It often is, because the bot
commits remotely. Did the run actually fire? And for the chart specifically,
was the exchange even open — a single market flat while the others printed is
a closed exchange, not a broken feed. The UK bank holidays that shut the LSE
and nothing else are the usual cause: early May, late May, the last Monday in
August, and the Christmas/New Year pair. To tell those apart in seconds, fetch
the published `market.json` and read the newest bar per index; if only one is
old the exchange was shut, if all are old the refresh did not run.

## Verifying a change to the page

`site/index.html` is one self-contained file with no build step and no tests,
so "it looks right in the diff" is not verification. Render it: serve `site/`
with `data/funds.json` and `data/market.json` copied in alongside it, load it
in a browser, and check the section you changed against the real data. Force
any edge cases in the data through the same render rather than reasoning about
them.

Which browser depends on where the session is running. In a Claude Code web
sandbox, Chromium is pre-installed at `/opt/pw-browsers/chromium`. On Tom's
Windows machine that path does not exist and neither does Playwright; use
headless Edge at
`C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe` with
`--headless=new --disable-gpu --virtual-time-budget=9000`, and prefer
`--dump-dom` over `--screenshot` when what you need to check is content rather
than layout. Headless Edge cannot click and always shoots from the top of the
page, so to reach a block behind a click or far down, write a scratchpad copy
of `index.html` with a probe script that calls the page's own functions and
moves the element of interest to the top of an emptied body. Never add that
probe to the real file.

Note that the live site cannot be fetched from inside a Claude Code web
session — the network policy blocks `jikkles.github.io` — so confirm a deploy
through the Actions API, not by loading the page.

## Never invent a figure

The desk's core rule, and it applies to code as much as to data. A value of
`"not yet verified"` means absent: show nothing, or say it is unverified.
Never lift a number out of a qualifier — `"not yet verified (strategy
typically rated 6 of 7)"` is not a 6 — and never fill a gap with a plausible
estimate. A visible gap is correct; a confident wrong number is not.
