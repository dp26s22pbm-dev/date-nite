#!/usr/bin/env bash
# Wait until no earlier Add Venue, Add Menu or Add City run is queued or in progress, so the runs that edit venues.json go one
# at a time, first come first served. (GitHub's own "concurrency" setting keeps only one waiting run and cancels the
# rest, which would drop a venue when three links arrive in a minute.)
# Runs on a failed lookup or after WAIT_LIMIT_SECONDS rather than blocking forever.
set -u
repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
run_id="${GITHUB_RUN_ID:?GITHUB_RUN_ID is not set}"
limit="${WAIT_LIMIT_SECONDS:-2700}"
poll="${WAIT_POLL_SECONDS:-15}"

mine=$(gh api "repos/$repo/actions/runs/$run_id" --jq .created_at) || { echo "Could not read this run's start time; not waiting."; exit 0; }

waited=0
while true; do
  rows=""
  for workflow in add-venue.yml add-menu.yml add-city.yml; do
    for status in queued in_progress; do
      got=$(gh api "repos/$repo/actions/workflows/$workflow/runs?status=$status&per_page=100" --jq '.workflow_runs[] | "\(.created_at) \(.id)"') || { echo "Could not list $workflow runs; not waiting."; exit 0; }
      rows+="$got"$'\n'
    done
  done
  # An earlier run started before this one (ties go to the lower run id)
  earlier=$(printf '%s' "$rows" | awk -v mine="$mine" -v me="$run_id" 'NF == 2 && $2 != me && ($1 < mine || ($1 == mine && $2 + 0 < me + 0))' | wc -l | tr -d ' ')
  if [ "$earlier" -eq 0 ]; then
    echo "No earlier run is waiting or running; going ahead after ${waited}s."
    exit 0
  fi
  if [ "$waited" -ge "$limit" ]; then
    echo "Still $earlier earlier run(s) after ${waited}s; going ahead anyway."
    exit 0
  fi
  echo "Waiting for $earlier earlier run(s) (${waited}s so far)."
  sleep "$poll"
  waited=$((waited + poll))
done
