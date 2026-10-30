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
import math
import tempfile
import datetime
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
BRANCH = "places-usage"
DATA_FILE = "usage.jsonl"
# The daily budget the report counts down from (Google's own per-day quota, if one is set, should match)
DAILY_LIMIT = int(os.environ.get("PLACES_DAILY_LIMIT", "500"))

# Which billing SKU a Text Search request falls under depends on the most expensive field in its field mask. The field
# tiers and monthly free allowances below follow Google's Places API (New) pricing and data-fields pages (checked
# Oct 2026); recheck them if Google changes its pricing.
ENTERPRISE_FIELDS = {"places.currentOpeningHours", "places.currentSecondaryOpeningHours", "places.internationalPhoneNumber",
                     "places.nationalPhoneNumber", "places.priceLevel", "places.priceRange", "places.rating",
                     "places.regularOpeningHours", "places.regularSecondaryOpeningHours", "places.transitStation",
                     "places.userRatingCount", "places.websiteUri"}
ATMOSPHERE_FIELDS = {"places.allowsDogs", "places.curbsidePickup", "places.delivery", "places.dineIn", "places.editorialSummary",
                     "places.evChargeAmenitySummary", "places.evChargeOptions", "places.fuelOptions", "places.generativeSummary",
                     "places.goodForChildren", "places.goodForGroups", "places.goodForWatchingSports", "places.liveMusic",
                     "places.menuForChildren", "places.neighborhoodSummary", "places.outdoorSeating", "places.parkingOptions",
                     "places.paymentOptions", "places.reservable", "places.restroom", "places.reviews", "places.reviewSummary",
                     "places.routingSummaries", "places.servesBeer", "places.servesBreakfast", "places.servesBrunch",
                     "places.servesCocktails", "places.servesCoffee", "places.servesDessert", "places.servesDinner",
                     "places.servesLunch", "places.servesVegetarianFood", "places.servesWine", "places.takeout"}
# Fractions of the daily limit that post a comment on the "Places usage alerts" issue when a run first crosses them
ALERT_AT = (0.5, 0.9)
ALERT_ISSUE = "Places usage alerts"
FREE_MONTHLY = {"Text Search Pro": 5000, "Text Search Enterprise": 1000, "Text Search Enterprise + Atmosphere": 1000}

def log_path():
    return os.environ.get("PLACES_USAGE_LOG") or os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "places-usage.jsonl")

def sku_for(field_mask):
    fields = {f.strip() for f in field_mask.split(",")}
    if fields & ATMOSPHERE_FIELDS:
        return "Text Search Enterprise + Atmosphere"
    return "Text Search Enterprise" if fields & ENTERPRISE_FIELDS else "Text Search Pro"

def sku_of(entry):
    """The SKU a logged request belongs to. Lines written before Atmosphere fields were told apart carry no "v": the
    Audit Venues workflow asked for them, so its lines are counted as Atmosphere."""
    if "v" not in entry and entry.get("workflow") == "Audit Venues" and entry["sku"] == "Text Search Enterprise":
        return "Text Search Enterprise + Atmosphere"
    return entry["sku"]

def record(field_mask, status):
    """Append one request to the run's log. `status` is the HTTP status, or 0 when no answer came back.
    Never raises: a logging problem must not stop a venue from being added."""
    try:
        entry = {
            "t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "sku": sku_for(field_mask), "status": status, "v": 2,
            "run": os.environ.get("GITHUB_RUN_ID", ""), "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
        }
        with open(log_path(), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Could not log the Places request: {e}")

def billed_today(lines):
    """How many of these log lines are requests Google answered today (Pacific time)."""
    today = datetime.datetime.now(PACIFIC).date()
    count = 0
    for line in lines:
        try:
            e = json.loads(line)
            if e["status"] == 200 and datetime.datetime.fromisoformat(e["t"]).astimezone(PACIFIC).date() == today:
                count += 1
        except (ValueError, KeyError):
            continue
    return count

def alert(before, after):
    """Comment on the standing alert issue (opened if missing) when the day's count crossed 50% or 90% of the limit.
    GitHub then notifies the repository owner by email and in the app. Never raises."""
    crossed = [f for f in ALERT_AT if before < math.ceil(DAILY_LIMIT * f) <= after]
    if not crossed:
        return
    try:
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
        level = f"{round(max(crossed) * 100)}%"
        body = (f"@{owner} " if owner else "") + (f"Google Places use passed {level} of today's budget: {after} of {DAILY_LIMIT} "
                f"requests used, {max(DAILY_LIMIT - after, 0)} left. The day resets at midnight Pacific time. "
                f"Details: {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/workflows/places-usage.yml")
        found = subprocess.run(["gh", "issue", "list", "--state", "open", "--search", f'"{ALERT_ISSUE}" in:title',
                                "--json", "number,title", "--limit", "10"], capture_output=True, text=True)
        number = next((i["number"] for i in json.loads(found.stdout or "[]") if i["title"] == ALERT_ISSUE), None)
        if number is None:
            made = subprocess.run(["gh", "issue", "create", "--title", ALERT_ISSUE, "--body",
                                   "Each comment here is a warning that the day's Google Places use passed 50% or 90% of its budget. "
                                   "Subscribe to this issue to be notified."], capture_output=True, text=True, check=True)
            number = made.stdout.strip().rsplit("/", 1)[-1]
        subprocess.run(["gh", "issue", "comment", str(number), "--body", body], capture_output=True, text=True, check=True)
        print(f"Posted the {level} Places usage alert on issue {number}.")
    except Exception as e:
        print(f"Could not post the Places usage alert: {e}")

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
        data_path = os.path.join(work, DATA_FILE)
        before = 0
        if os.path.exists(data_path):
            with open(data_path) as f:
                before = billed_today(f.read().splitlines())
        with open(data_path, "a") as f:
            f.write("\n".join(lines) + "\n")
        git("add", DATA_FILE, cwd=work)
        message = f"Log {len(lines)} Places request(s) from run {os.environ.get('GITHUB_RUN_ID', 'local')}"
        if git(*ident, "commit", "--quiet", "-m", message, cwd=work).returncode == 0:
            pushed = git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=work)
            if pushed.returncode == 0:
                os.remove(path)
                git("worktree", "remove", "--force", work)
                print(f"Logged {len(lines)} Places request(s) to the {BRANCH} branch.")
                alert(before, before + billed_today(lines))
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

def remaining_today():
    """Requests left in today's budget, from the log on the places-usage branch (requests this run has made but not yet
    saved are not included). A log that cannot be read counts as nothing used."""
    entries = load_entries()
    return max(DAILY_LIMIT - billed_today(json.dumps(e) for e in entries), 0)

def report(entries, recent=15):
    now = datetime.datetime.now(PACIFIC)
    for e in entries:
        e["when"] = datetime.datetime.fromisoformat(e["t"]).astimezone(PACIFIC)
    billed = [e for e in entries if e["status"] == 200]
    today = [e for e in billed if e["when"].date() == now.date()]
    month = [e for e in billed if (e["when"].year, e["when"].month) == (now.year, now.month)]

    out = [f"Google Places usage, {now:%A %B %-d, %Y} (Pacific time; the daily quota resets at midnight)", ""]
    out.append(f"Today: {len(today)} of {DAILY_LIMIT} requests used, {max(DAILY_LIMIT - len(today), 0)} left")
    for sku in sorted({sku_of(e) for e in today}):
        out.append(f"  {sku}: {sum(sku_of(e) == sku for e in today)}")
    if not today:
        out.append("  (no requests yet)")

    out += ["", f"Free monthly allowance left ({now:%B})"]
    for sku, allowance in FREE_MONTHLY.items():
        used = sum(sku_of(e) == sku for e in month)
        out.append(f"  {sku}: {max(allowance - used, 0):,} of {allowance:,}")

    out += ["", f"Recent requests (newest first, {recent} shown)"]
    for e in sorted(entries, key=lambda e: e["when"], reverse=True)[:recent]:
        result = "ok" if e["status"] == 200 else f"failed ({e['status'] or 'no answer'}), not counted"
        source = f"{e['workflow']} run {e['run']}" if e["run"] else e["workflow"]
        out.append(f"  {e['when']:%b %-d %-I:%M %p}  {sku_of(e)}  {result}  [{source}]")
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
