#!/usr/bin/env bash
# Bring the checked-out branch up to date with origin/main. A conflict in grid/grid.json alone is resolved with
# merge_grid.py (a cell either side added is kept). Any other conflict aborts the merge and returns 1, leaving the
# branch as it was. Returns 0 when the branch is up to date or merged. Run from the repository root with git user set.
# Used by refresh-cell-prs.yml (after main changes) and by search-cells.yml / split-cells.yml (just before their pull
# request opens: a run can take many minutes, so the main it started from may be stale by then, and a pull request
# created by the workflow token does not start refresh-cell-prs.yml, so nothing else would repair it).
set -u
branch=$(git rev-parse --abbrev-ref HEAD)
git fetch origin main || { echo "Could not fetch main."; exit 1; }
if git merge-base --is-ancestor origin/main HEAD; then
  echo "Already up to date."
  exit 0
fi
if git merge --no-edit -m "Merge main into $branch" origin/main; then
  exit 0
fi
others=$(git diff --name-only --diff-filter=U | grep -vx 'grid/grid.json' || true)
if [ -n "$others" ] || ! git ls-files -u grid/grid.json | grep -q .; then
  echo "::warning::$branch has conflicts beyond grid/grid.json ($others); left for a person."
  git merge --abort
  exit 1
fi
tmp="${RUNNER_TEMP:-/tmp}"
git show :1:grid/grid.json > "$tmp/base.json" 2>/dev/null || echo '{}' > "$tmp/base.json"
git show :2:grid/grid.json > "$tmp/branch.json"
git show :3:grid/grid.json > "$tmp/main.json"
if ! python merge_grid.py "$tmp/base.json" "$tmp/branch.json" "$tmp/main.json" grid/grid.json; then
  echo "::warning::Could not merge grid/grid.json on $branch; left for a person."
  git merge --abort
  exit 1
fi
git add grid/grid.json
git commit -q --no-edit -m "Merge main into $branch (resolve grid.json conflict)"
