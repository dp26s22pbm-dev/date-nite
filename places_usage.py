"""Count the Google Places requests this repository makes, and show how many are left for the day.

Every Places request made by add_venue_from_issue.py and add_city.py is logged with record(). The workflows run on
throwaway runners, so each run ends with `python places_usage.py publish`, which appends its log to usage.jsonl on the
places-usage branch (kept off main so it never triggers a site rebuild). Read it back with:

    python places_usage.py report [--recent N]

Only requests Google answered with HTTP 200 count as used. Counts cover this repository's calls only; Google's Cloud
Console is still the record of everything made with the key. The Places daily quota resets at midnight Pacific time."""
import os
import sys
import json
import subprocess
import tempfile
import datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
BRANCH = "places-usage"
DATA_FILE = "usage.jsonl"
# The daily budget the report counts down from (Google's own per-day quota, if one is set, should match)
DAILY_LIMIT = int(os.environ.get("PLACES_DAILY_LIMIT", "500"))

# Which billing SKU a Text Search request falls under depends on the most expensive field in its field mask.
# Check these tiers and the monthly free allowances below against Google's current pricing page; they have changed before.
ENTERPRISE_FIELDS = {"places.websiteUri", "places.rating", "places.userRatingCount", "places.nationalPhoneNumber",
                     "places.internationalPhoneNumber", "places.regularOpeningHours", "places.priceLevel"}
FREE_MONTHLY = {"Text Search Pro": 5000, "Text Search Enterprise": 1000}

def log_path():
    return os.environ.get("PLACES_USAGE_LOG") or os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "places-usage.jsonl")

def sku_for(field_mask):
    fields = {f.strip() for f in field_mask.split(",")}
    return "Text Search Enterprise" if fields & ENTERPRISE_FIELDS else "Text Search Pro"

def record(field_mask, status, query):
    """Append one request to the run's log. `status` is the HTTP status, or 0 when no answer came back.
    Never raises: a logging problem must not stop a venue from being added."""
    try:
        entry = {
            "t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "sku": sku_for(field_mask), "status": status, "query": (query or "")[:80],
            "run": os.environ.get("GITHUB_RUN_ID", ""), "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
        }
        with open(log_path(), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Could not log the Places request: {e}")

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)

def publish():
    """Append this run's log to usage.jsonl on the places-usage branch (created on first use)."""
    path = log_path()
    if not os.path.exists(path) or not os.path.getsize(path):
        print("No Places requests to log.")
        return
    with open(path) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    ident = ["-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com"]
    work = os.path.join(tempfile.mkdtemp(), "usage")
    for attempt in range(1, 7):
        if git("worktree", "prune").returncode:
            break
        have_branch = git("fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}").returncode == 0
        if git("worktree", "add", "--detach", work, f"origin/{BRANCH}" if have_branch else "HEAD").returncode:
            break
        if not have_branch:
            # First use: start a history of its own, holding nothing but the log
            git("checkout", "--orphan", BRANCH, cwd=work)
            git("rm", "-rf", "--quiet", ".", cwd=work)
        with open(os.path.join(work, DATA_FILE), "a") as f:
            f.write("\n".join(lines) + "\n")
        git("add", DATA_FILE, cwd=work)
        message = f"Log {len(lines)} Places request(s) from run {os.environ.get('GITHUB_RUN_ID', 'local')}"
        if git(*ident, "commit", "--quiet", "-m", message, cwd=work).returncode == 0:
            pushed = git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=work)
            if pushed.returncode == 0:
                os.remove(path)
                git("worktree", "remove", "--force", work)
                print(f"Logged {len(lines)} Places request(s) to the {BRANCH} branch.")
                return
            print(f"Push rejected (attempt {attempt}): {pushed.stderr.strip()[-200:]}")
        git("worktree", "remove", "--force", work)
    print("Could not save the Places log; the requests from this run are not counted.")

def load_entries():
    """Every logged request, read from the places-usage branch on origin."""
    if git("fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}").returncode:
        return []
    shown = git("show", f"origin/{BRANCH}:{DATA_FILE}")
    entries = []
    for line in shown.stdout.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries

def report(entries, recent=15):
    now = datetime.datetime.now(PACIFIC)
    for e in entries:
        e["when"] = datetime.datetime.fromisoformat(e["t"]).astimezone(PACIFIC)
    billed = [e for e in entries if e["status"] == 200]
    today = [e for e in billed if e["when"].date() == now.date()]
    month = [e for e in billed if (e["when"].year, e["when"].month) == (now.year, now.month)]

    out = [f"Google Places usage, {now:%A %B %-d, %Y} (Pacific time; the daily quota resets at midnight)", ""]
    out.append(f"Today: {len(today)} of {DAILY_LIMIT} requests used, {max(DAILY_LIMIT - len(today), 0)} left")
    for sku in sorted({e["sku"] for e in today}):
        out.append(f"  {sku}: {sum(e['sku'] == sku for e in today)}")
    if not today:
        out.append("  (no requests yet)")

    out += ["", f"Free monthly allowance left ({now:%B})"]
    for sku, allowance in FREE_MONTHLY.items():
        used = sum(e["sku"] == sku for e in month)
        out.append(f"  {sku}: {max(allowance - used, 0):,} of {allowance:,}")

    out += ["", f"Recent requests (newest first, {recent} shown)"]
    for e in sorted(entries, key=lambda e: e["when"], reverse=True)[:recent]:
        result = "ok" if e["status"] == 200 else f"failed ({e['status'] or 'no answer'}), not counted"
        source = f"{e['workflow']} run {e['run']}" if e["run"] else e["workflow"]
        out.append(f"  {e['when']:%b %-d %-I:%M %p}  {e['sku']}  {result}  \"{e['query']}\"  [{source}]")
    if not entries:
        out.append("  (nothing logged yet)")
    return "\n".join(out)

def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "publish":
        publish()
    elif command == "report":
        recent = int(sys.argv[sys.argv.index("--recent") + 1]) if "--recent" in sys.argv else 15
        print(report(load_entries(), recent))
    else:
        sys.exit("usage: places_usage.py [report [--recent N] | publish]")

if __name__ == "__main__":
    main()
