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

## Pushing to `main` publishes the site

`.github/workflows/daily-update.yml` runs on every push to `main` (as well as
on its 06:23 UTC schedule). It is not just a page publish: it re-fetches NAVs
for all ~70 funds, rebuilds `funds.json`, commits the refreshed data and then
assembles and deploys the Pages artifact. Expect minutes, not seconds, and
expect the run to add its own data commit on top of yours.

So a push to `main` is a deploy to a live site. Get the change right before
pushing rather than pushing to see what happens.

## Verifying a change to the page

`site/index.html` is one self-contained file with no build step and no tests,
so "it looks right in the diff" is not verification. Render it: serve `site/`
with `data/funds.json` and `data/market.json` copied in alongside it, load it
in Chromium (pre-installed at `/opt/pw-browsers/chromium`), and check the
section you changed against the real data. Force any edge cases in the data
through the same render rather than reasoning about them.

Note that the live site cannot be fetched from inside a Claude Code web
session — the network policy blocks `jikkles.github.io` — so confirm a deploy
through the Actions API, not by loading the page.

## Never invent a figure

The desk's core rule, and it applies to code as much as to data. A value of
`"not yet verified"` means absent: show nothing, or say it is unverified.
Never lift a number out of a qualifier — `"not yet verified (strategy
typically rated 6 of 7)"` is not a 6 — and never fill a gap with a plausible
estimate. A visible gap is correct; a confident wrong number is not.
