#!/usr/bin/env bash
# Build the Pages artifact in _site/. The ONE copy of this step - every
# workflow that deploys runs this script rather than its own inline block.
#
# It was three hand-written copies, one per workflow, and they drifted: only
# the daily run copied movers.json, so each hourly or weekly deploy published
# a site without it. The panel 404ed and printed "the constituent rankings
# could not be loaded" until the next daily run happened to put the file back
# - a page that broke and healed on a schedule, from a one-line omission
# nothing checked for. One script cannot drift from itself.
#
# Run from anywhere: it works from the repo root. Exits non-zero, and the
# workflow fails before deploying, if any file is missing or fails the gate.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p _site
cp site/index.html _site/index.html

# Stamp the deployed copy with the commit it was built from. The page compares
# this against the served file to tell whether the copy the browser is holding
# has been superseded - see newBuildAvailable(). Only _site is stamped; the
# repo keeps its placeholder, which is how the page knows it is being opened
# locally rather than served. HEAD rather than $GITHUB_SHA, which is only the
# commit the run was created on: a queued run building a newer index.html
# under that old stamp would tell a browser holding the old page nothing had
# changed.
sed -i "s/__BUILD__/$(git rev-parse HEAD)/" _site/index.html

# Every file index.html fetches. MISSING ONE IS A HARD FAILURE.
#
# constituents.json is deliberately not here: index_movers.py reads it to
# build movers.json, and the page never fetches it.
#
# The two files carrying the desk's own prose - fund notes, caveats, the run
# report's warnings - go through the anonymisation gate on the way in, on EVERY
# deploy. Before this, the gate ran only in the daily and weekly workflows, so
# the hourly chart refresh published whatever funds.json main held, unchecked:
# a hand commit to it went live within hours without ever meeting the gate.
#
# The other three are built by scripts from third-party feeds (Yahoo,
# Wikipedia, NS&I) and carry no maintainer text. They are copied as they are:
# the gate's internal-process word list includes "cursor" and "copilot", and a
# company of that name joining an index must not be able to stop every deploy.
for f in funds.json last_run.md; do
  test -f "data/$f" || { echo "::error::data/$f missing - refusing to deploy a site without it"; exit 1; }
  python scripts/anonymise.py "data/$f" "_site/$f" \
    || { echo "::error::data/$f failed the anonymisation gate - not deploying"; exit 1; }
done
for f in market.json movers.json premium_bonds.json; do
  test -f "data/$f" || { echo "::error::data/$f missing - refusing to deploy a site without it"; exit 1; }
  cp "data/$f" "_site/$f"
done

echo "assembled _site: $(ls _site | tr '\n' ' ')"
