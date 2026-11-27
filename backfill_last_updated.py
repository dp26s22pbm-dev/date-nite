"""One-off: fill in `lastUpdated` for venues that have none, from the git history of venues.json.

The date used is the day of the last commit that changed one of the venue's price fields
(dashboard_stats.PRICE_FIELDS; a city/state cleanup or a version stamp does not count). For a venue whose prices
never changed since it was added, that is the day it was added. That is "when these prices were last written", which is
what the popup's "Updated" line means. It cannot tell whether someone re-checked the prices and found no change.

Venues that already have a lastUpdated are never touched. Venues with no history at all are left blank.

    python backfill_last_updated.py            # dry run: prints what it would write, changes nothing
    python backfill_last_updated.py --write    # writes venues.json

Needs the full git history (a shallow clone would date every old venue to the oldest commit it can see), so run it
after a full checkout (actions/checkout with fetch-depth: 0).
"""
import collections
import datetime
import json
import subprocess
import sys

import dashboard_stats as stats

VENUES_FILE = stats.VENUES_FILE


def is_shallow():
    out = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], capture_output=True, text=True)
    return out.stdout.strip() == "true"


def main():
    write = "--write" in sys.argv
    if is_shallow():
        sys.exit("This checkout is shallow, so the history is incomplete. Fetch the full history first (git fetch --unshallow).")

    with open(VENUES_FILE) as f:
        venues = json.load(f)
    hist, commits = stats.venue_history(max_commits=5000)
    print(f"Read {commits} commits that changed {VENUES_FILE}; {len(venues)} venues.")

    today = datetime.date.today().isoformat()
    filled, already, no_history = [], 0, []
    for v in venues:
        if v.get("lastUpdated"):
            already += 1
            continue
        day = hist.get(stats.venue_key(v), {}).get("last")
        if not day or day > today:
            no_history.append(v.get("name"))
            continue
        filled.append((v, day))

    print(f"Already dated: {already}. Would fill: {len(filled)}. No history found: {len(no_history)}.")
    by_day = collections.Counter(day for _, day in filled)
    print("Dates it would write (day: venues):")
    for day, n in sorted(by_day.items()):
        print(f"  {day}: {n}")
    for v, day in filled[:12]:
        print(f"  e.g. {day}  {v.get('name')} ({v.get('city')}, {v.get('state')})")
    if no_history:
        print("Left blank:", ", ".join(map(str, no_history[:10])))

    if not write:
        print("Dry run. Nothing written. Add --write to apply.")
        return
    for v, day in filled:
        v["lastUpdated"] = day
    with open(VENUES_FILE, "w") as f:
        json.dump(venues, f, indent=2)
    print(f"Wrote {len(filled)} dates to {VENUES_FILE}.")


if __name__ == "__main__":
    main()
