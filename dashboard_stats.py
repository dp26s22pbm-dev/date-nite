"""Numbers for the Date Nite dashboard, saved as one JSON file so a page can show them without touching the repository.

    python dashboard_stats.py build [--out dashboard-stats.json]    compute every section and write the file
    python dashboard_stats.py publish [--file dashboard-stats.json] save the file on the dashboard-data branch

The dashboard-data branch is kept off main, like menu-log and places-usage, so saving it never triggers a site rebuild.
It holds a single commit (each publish replaces it) with dashboard-stats.json and nothing else.

Sections (each one is computed on its own; a source that fails leaves {"error": "..."} in its section and the rest
still ships):
    venues      total, added and changed in the last 7 days, stale (last change over 30 days ago), age from git history
    grid        cells searched, still to search, coverage of the searchable cells, full cells due to split
    menu        imports this week vs last week, outcomes, success rate, venues with no real menu, failure causes
    review      open issues and pull requests that wait on a person (needs-menu, double-check, venue-review, grid PRs)
    workflows   failed GitHub Actions runs in the last 7 days, by workflow
    places      Google Places requests used today and over the last 7 days against the daily budget
    history     one small row per run, so the page can show a trend

"Changed" for a venue means a price or its lastUpdated date differs from the commit before (see PRICE_FIELDS; a bulk
cleanup of city and state names does not count). A venue's age is the later of its lastUpdated date and the date of the last commit that changed it,
because most venues on the map have no lastUpdated field.

Needs: git history of main (fetch-depth 0) and the menu-log and places-usage branches fetched. The review and
workflows sections need the gh CLI with GH_TOKEN; without it they report an error and the rest is unaffected."""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter

import grid_cells
import menu_log
import places_usage

BRANCH = "dashboard-data"
DATA_FILE = "dashboard-stats.json"
STALE_DAYS = 30
WINDOW_DAYS = 7
HISTORY_ROWS = 60
VENUES_FILE = "venues.json"
# A venue counts as changed when one of these differs from the commit before. Town, state, coordinates and the like are
# left out so a bulk data cleanup (one commit rewrote city and state on 119 venues) does not make every venue look fresh.
PRICE_FIELDS = ("app", "casualMain", "premiumMain", "drink", "dessert", "lastUpdated")
REVIEW_LABELS = ("needs-menu", "double-check", "venue-review")
GRID_PR_PREFIX = "Add venues for cells: "

UTC = datetime.timezone.utc


def now_utc():
    return datetime.datetime.now(UTC)


def run(*args, cwd=None, check=True):
    out = subprocess.run(list(args), capture_output=True, text=True, cwd=cwd, timeout=300)
    if check and out.returncode:
        raise RuntimeError(f"{' '.join(args[:3])}: {out.stderr.strip()[-200:]}")
    return out


def parse_time(text):
    try:
        t = datetime.datetime.fromisoformat((text or "").replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        return None


def section(fn, *args):
    """Run one section; a failure becomes {"error": ...} instead of stopping the build."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - any failure must stay inside its own section
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- venues

def venue_key(v):
    return str(v.get("id")) if v.get("id") is not None else f"{v.get('name')}|{v.get('city')}"


def venue_history(max_commits=600):
    """{venue key: {"first": date, "last": date}} from the commits that changed venues.json, oldest first."""
    log = run("git", "log", f"--max-count={max_commits}", "--format=%H %cI", "--", VENUES_FILE).stdout.split("\n")
    commits = [line.split(" ", 1) for line in log if line.strip()][::-1]
    seen, prev = {}, {}
    for sha, when in commits:
        shown = run("git", "show", f"{sha}:{VENUES_FILE}", check=False)
        if shown.returncode:
            continue
        try:
            venues = {venue_key(v): v for v in json.loads(shown.stdout)}
        except ValueError:
            continue
        day = when[:10]
        for k, v in venues.items():
            if k not in seen:
                seen[k] = {"first": day, "last": day}
            elif any(prev.get(k, {}).get(f) != v.get(f) for f in PRICE_FIELDS):
                seen[k]["last"] = day
        prev = venues
    return seen, len(commits)


def venues_section(now):
    with open(VENUES_FILE) as f:
        venues = json.load(f)
    hist, commits = venue_history()
    today = now.date()
    week_ago = (now - datetime.timedelta(days=WINDOW_DAYS)).date().isoformat()
    stale_cut = (now - datetime.timedelta(days=STALE_DAYS)).date().isoformat()
    added = changed = dated_by_field = 0
    stale, ages = [], []
    for v in venues:
        k = venue_key(v)
        h = hist.get(k, {})
        last = max(filter(None, [v.get("lastUpdated"), h.get("last")]), default=None)
        if v.get("lastUpdated"):
            dated_by_field += 1
        if h.get("first", "") >= week_ago and h.get("first"):
            added += 1
        elif h.get("last", "") >= week_ago and h.get("last"):
            changed += 1
        if last:
            ages.append((today - datetime.date.fromisoformat(last)).days)
            if last < stale_cut:
                stale.append({"name": v.get("name"), "city": v.get("city"), "last_changed": last})
    stale.sort(key=lambda s: s["last_changed"])
    return {
        "total": len(venues), "added_7d": added, "changed_7d": changed, "stale_days": STALE_DAYS,
        "stale_count": len(stale), "stale_oldest": stale[:15], "age_unknown": len(venues) - len(ages),
        "with_lastUpdated_field": dated_by_field, "median_age_days": sorted(ages)[len(ages) // 2] if ages else None,
        "history_commits_read": commits,
    }


# ---------------------------------------------------------------- grid

def grid_section(now):
    grid = grid_cells.load_grid()
    with open(VENUES_FILE) as f:
        venues = json.load(f)
    n = grid["splitInto"]
    skip, split, searched = set(grid.get("skip", [])), set(grid.get("split", [])), set(grid.get("searched", []))
    base_cells = grid["cols"] * grid["rows"]
    units = base_cells - len(skip) - len(split) + len(split) * n * n
    done = len([s for s in searched if s not in skip and s not in split])
    per_cell = Counter()
    for v in venues:
        c = v.get("coords")
        cell = grid_cells.cell_of(grid, c, split) if c else None
        if cell:
            per_cell[cell] += 1
    full_at = grid.get("fullAt", 3)
    due_split = sorted((c for c, k in per_cell.items() if k >= full_at and "-" not in c and c not in split),
                       key=grid_cells.sort_key)
    return {
        "cells_total": base_cells, "skipped": len(skip), "split": len(split), "searchable": units,
        "searched": done, "remaining": max(units - done, 0), "coverage_pct": round(100 * done / units, 1) if units else None,
        "full_at": full_at, "full_cells_due_to_split": due_split, "venues_outside_grid": len(venues) - sum(per_cell.values()),
    }


# ---------------------------------------------------------------- menu log

def failure_cause(entry):
    """One plain-language cause for a venue whose import ended badly, picked from its steps in a fixed order."""
    steps = [(s.get("step"), s.get("result")) for s in entry.get("steps", [])]
    results = {r for _, r in steps}
    if entry.get("outcome") == "reply_unreadable":
        return "A pasted menu could not be read"
    if "bot_challenge" in results:
        return "The restaurant's site blocks automated reading (bot check)"
    if ("fetch", "http_403") in steps:
        return "The restaurant's site refused the request (403)"
    if not entry.get("host"):
        return "No website to read a menu from"
    if any(r in ("too_few_prices", "no_casual_main", "no_prices_links_followed", "lunch_prices_suspected") for r in results):
        return "A menu page was found but no usable prices could be read"
    if ("site_pages", "no_menu_page_found") in steps:
        return "No menu page could be found on the site"
    if any(s == "pdf" for s, _ in steps):
        return "A PDF or image menu could not be read"
    return "Other"


def menu_section(now):
    entries = menu_log.load_entries()
    if not entries:
        return {"error": "menu-log branch has no entries"}
    cur = menu_log.within_days(entries, WINDOW_DAYS, now)
    prev = menu_log.within_days(entries, WINDOW_DAYS, now, offset=WINDOW_DAYS)

    def summary(rows):
        out = Counter(e.get("outcome") for e in rows)
        n = len(rows)
        return {"imports": n, "outcomes": dict(out), "menu_read_pct": round(100 * out["menu_read"] / n) if n else None}

    problems = [e for e in cur if e.get("outcome") in menu_log.PROBLEM_OUTCOMES]
    causes = Counter(failure_cause(e) for e in problems)
    steps = Counter(r for e in cur for r in menu_log._failures(e))
    latest = {}
    for e in sorted(entries, key=lambda e: e.get("t", "")):
        latest[(e.get("venue") or "").lower()] = e
    unresolved = sorted((e for e in latest.values() if e.get("outcome") in menu_log.UNRESOLVED_OUTCOMES),
                        key=lambda e: e["t"], reverse=True)
    tokens_in = tokens_out = 0
    for e in cur:
        for s in e.get("steps", []):
            if s.get("step") == "search_claude":
                tokens_in += s.get("tokens_in") or 0
                tokens_out += s.get("tokens_out") or 0
    return {
        "this_week": summary(cur), "last_week": summary(prev),
        "problem_venues_this_week": len(problems),
        "top_causes": [{"cause": c, "venues": n} for c, n in causes.most_common(3)],
        "all_causes": [{"cause": c, "venues": n} for c, n in causes.most_common()],
        "top_step_reasons": [{"step": s, "result": r, "count": n} for (s, r), n in steps.most_common(5)],
        "step_reasons_note": "Raw attempt counts. fetch/http_404 includes real dead links, so judge by venues, not attempts.",
        "no_real_menu": [{"venue": e["venue"], "site": e.get("host"), "outcome": e["outcome"], "date": e["t"][:10]} for e in unresolved],
        "no_real_menu_count": len(unresolved),
        "partial_reads_this_week": sum(1 for e in cur if e.get("outcome") == "menu_read_partial"),
        "pasted_menus_this_week": sum(1 for e in cur if e.get("reply") and e.get("outcome") == "menu_read"),
        "claude_search_tokens_in": tokens_in, "claude_search_tokens_out": tokens_out,
        "log_records_total": len(entries), "latest_record": entries[-1]["t"] if entries else None,
    }


# ---------------------------------------------------------------- GitHub (needs the gh CLI)

def repo_slug():
    slug = os.environ.get("GITHUB_REPOSITORY")
    if slug:
        return slug
    url = run("git", "remote", "get-url", "origin").stdout.strip()
    return url.rstrip("/").removesuffix(".git").split("github.com/")[-1].split("github.com:")[-1]


def gh_api(path, **params):
    """GET a REST list from the repository one page at a time (REST only, no GraphQL: it works through proxies that
    block that, and the explicit pages avoid the numeric-ID links some proxies refuse)."""
    rows = []
    for page in range(1, 11):
        query = "&".join(f"{k}={v}" for k, v in {**params, "per_page": 100, "page": page}.items())
        data = json.loads(run("gh", "api", f"repos/{repo_slug()}/{path}?{query}").stdout or "[]")
        got = data if isinstance(data, list) else data.get("workflow_runs", [])
        rows.extend(got)
        if len(got) < 100:
            break
    return rows


def review_section(now):
    issues = [i for i in gh_api("issues", state="open") if "pull_request" not in i]
    waiting = []
    for i in issues:
        names = [l["name"] for l in i.get("labels", [])]
        hit = [n for n in REVIEW_LABELS if n in names]
        if hit:
            waiting.append({"kind": hit[0], "number": i["number"], "title": i["title"], "url": i["html_url"],
                            "opened": i["created_at"][:10], "labels": names})
    prs = gh_api("pulls", state="open")
    brief = lambda p: {"number": p["number"], "title": p["title"], "url": p["html_url"], "opened": p["created_at"][:10]}
    grid_prs = [brief(p) for p in prs if p["title"].startswith(GRID_PR_PREFIX)]
    other_prs = [brief(p) for p in prs if not p["title"].startswith(GRID_PR_PREFIX)]
    return {"open_issues_total": len(issues), "waiting_issues": waiting,
            "waiting_by_kind": dict(Counter(w["kind"] for w in waiting)),
            "grid_prs": grid_prs, "other_prs": other_prs, "waiting_count": len(waiting) + len(grid_prs)}


def workflows_section(now):
    cutoff = now - datetime.timedelta(days=WINDOW_DAYS)
    runs = gh_api("actions/runs", status="completed", created=f">={cutoff.date().isoformat()}")
    recent = [r for r in runs if (parse_time(r["created_at"]) or cutoff) >= cutoff]
    failed = [r for r in recent if r.get("conclusion") == "failure"]
    by_name = {}
    for r in failed:
        row = by_name.setdefault(r["name"], {"workflow": r["name"], "failures": 0, "latest": "", "url": ""})
        row["failures"] += 1
        if r["created_at"] > row["latest"]:
            row["latest"], row["url"] = r["created_at"], r["html_url"]
    rows = sorted(by_name.values(), key=lambda r: (-r["failures"], r["workflow"]))
    for r in rows:
        r["latest"] = r["latest"][:10]
    return {"runs_7d": len(recent), "failed_7d": len(failed), "failed_by_workflow": rows}


# ---------------------------------------------------------------- Places usage

def places_section(now):
    entries = places_usage.load_entries()
    pacific = places_usage.PACIFIC
    today = now.astimezone(pacific).date()
    per_day = Counter()
    for e in entries:
        if e.get("status") != 200:
            continue
        t = parse_time(e.get("t"))
        if t:
            per_day[t.astimezone(pacific).date()] += 1
    days = [today - datetime.timedelta(days=i) for i in range(WINDOW_DAYS)][::-1]
    return {"daily_limit": places_usage.DAILY_LIMIT, "used_today": per_day.get(today, 0),
            "last_7_days": [{"date": d.isoformat(), "requests": per_day.get(d, 0)} for d in days],
            "total_7d": sum(per_day.get(d, 0) for d in days), "timezone": "America/Los_Angeles"}


# ---------------------------------------------------------------- build and publish

def previous_history():
    out = run("git", "fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}", check=False)
    if out.returncode:
        return []
    shown = run("git", "show", f"origin/{BRANCH}:{DATA_FILE}", check=False)
    try:
        return json.loads(shown.stdout).get("history", []) if shown.returncode == 0 else []
    except ValueError:
        return []


def history_row(stats, now):
    def get(*path):
        cur = stats
        for p in path:
            cur = cur.get(p) if isinstance(cur, dict) else None
        return cur
    return {"date": now.date().isoformat(), "venues": get("venues", "total"), "grid_remaining": get("grid", "remaining"),
            "coverage_pct": get("grid", "coverage_pct"), "waiting": get("review", "waiting_count"),
            "no_real_menu": get("menu", "no_real_menu_count"), "menu_read_pct": get("menu", "this_week", "menu_read_pct"),
            "failed_runs_7d": get("workflows", "failed_7d"), "places_used_today": get("places", "used_today")}


def build(out_path):
    now = now_utc()
    stats = {"generated_at": now.isoformat(timespec="seconds"), "window_days": WINDOW_DAYS,
             "repo": os.environ.get("GITHUB_REPOSITORY", "")}
    for name, fn in (("venues", venues_section), ("grid", grid_section), ("menu", menu_section),
                     ("review", review_section), ("workflows", workflows_section), ("places", places_section)):
        stats[name] = section(fn, now)
    history = [r for r in previous_history() if r.get("date") != now.date().isoformat()]
    history.append(history_row(stats, now))
    stats["history"] = history[-HISTORY_ROWS:]
    with open(out_path, "w") as f:
        json.dump(stats, f, indent=1)
        f.write("\n")
    bad = [k for k, v in stats.items() if isinstance(v, dict) and "error" in v]
    print(f"Wrote {out_path}." + (f" Sections with errors: {', '.join(bad)}." if bad else " Every section computed."))
    for k in bad:
        print(f"  {k}: {stats[k]['error']}")
    return stats


def publish(path):
    """Replace the dashboard-data branch with one commit holding the file. Retries if the push races another run."""
    ident = ["-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com"]
    for attempt in range(1, 5):
        work = os.path.join(tempfile.mkdtemp(), "dash")
        local = f"{BRANCH}-tmp-{os.getpid()}-{attempt}"  # never the real name, so a rerun in the same checkout cannot collide
        run("git", "worktree", "add", "--detach", work, "HEAD")
        try:
            run("git", "checkout", "--orphan", local, cwd=work)
            run("git", "rm", "-rf", "--quiet", ".", cwd=work, check=False)
            with open(path) as src, open(os.path.join(work, DATA_FILE), "w") as dst:
                dst.write(src.read())
            run("git", "add", DATA_FILE, cwd=work)
            run("git", *ident, "commit", "--quiet", "-m", f"Dashboard numbers for {now_utc().date().isoformat()}", cwd=work)
            pushed = run("git", "push", "--force", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=work, check=False)
            if pushed.returncode == 0:
                print(f"Saved {DATA_FILE} to the {BRANCH} branch.")
                return True
            print(f"Push rejected (attempt {attempt}): {pushed.stderr.strip()[-200:]}")
        finally:
            run("git", "worktree", "remove", "--force", work, check=False)
            run("git", "branch", "-D", local, check=False)
    print("Could not save the dashboard numbers.")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", default=DATA_FILE)
    p = sub.add_parser("publish")
    p.add_argument("--file", default=DATA_FILE)
    args = ap.parse_args()
    if args.cmd == "build":
        build(args.out)
    else:
        sys.exit(0 if publish(args.file) else 1)


if __name__ == "__main__":
    main()
