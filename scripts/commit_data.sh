#!/usr/bin/env bash
# Commit a run's data files to main. The ONE copy of this logic, shared by
# the daily and weekly workflows.
#
#   bash scripts/commit_data.sh "<commit message>" <file> [<file> ...]
#
# WHY IT IS SHAPED LIKE THIS
# --------------------------
# main can move while a run is working. Another bot run cannot do it - every
# workflow that commits shares the fund-tracker-pages concurrency group, and
# each checks out main as it starts - so what moves it is a person pushing.
#
# The two workflows used to answer that in opposite ways, and each was wrong
# in its own direction:
#
#   daily   reset onto the new main and committed its files on top. A hand
#           fix to funds.json pushed mid-run was silently reverted.
#   weekly  committed, then `git pull --rebase`. funds.json conflicts on any
#           overlapping edit, so the push failed and a 15-minute refresh of
#           ~400 pages was thrown away.
#
# Now: if any file this run is about to commit changed on main since the run
# checked out, refuse - loudly, and without touching main. The hand edit
# stands; the next run regenerates the data on top of it. Otherwise move onto
# the new main (--mixed, so the index is main's tree and only the named files
# are staged on top of it - anything else main gained is kept, not reverted)
# and commit.
set -euo pipefail
cd "$(dirname "$0")/.."

msg=$1
shift

git config user.name  "fund-tracker-bot"
git config user.email "actions@users.noreply.github.com"

# Nothing has been committed locally yet, so HEAD is still the commit this
# run checked out and computed its files from.
base=$(git rev-parse HEAD)
git fetch -q origin main

moved=$(git diff --name-only "$base" FETCH_HEAD -- "$@")
if [ -n "$moved" ]; then
  echo "::error::$(echo $moved) changed on main after this run started (it began from ${base:0:7}). Not committing over that change - the next run will regenerate this data on top of it."
  exit 1
fi

git reset -q --mixed FETCH_HEAD
git add -- "$@"
if git diff --staged --quiet; then
  echo "No changes to commit."
  exit 0
fi
git commit -q -m "$msg"
git push -q origin HEAD:main
echo "committed $(git rev-parse --short HEAD): $msg"
