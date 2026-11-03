"""A running record of how each venue's menu was (or was not) read, so a better menu importer can be built from real cases.

add_venue_from_issue.py calls start() when it begins looking for prices, step() each time one source succeeds or fails,
and finish() once it knows the outcome. That writes one JSON line per venue. The workflows run on throwaway runners, so
each run ends with `python menu_log.py publish`, which appends its lines to menu-log.jsonl on the menu-log branch (kept
off main so it never triggers a site rebuild; the same arrangement as places_usage.py). Read it back with:

    python menu_log.py report [--recent N]     problem sites, failure reasons and venues still without a menu
    python menu_log.py export                  every record as JSON lines, for analysis elsewhere

One record per venue per importer run:
    v, t, run, workflow, issue          when and where it ran
    venue, website, host, website_status, menu_url, reply
                                        the venue and what the importer had to work with. reply is true when someone
                                        pasted a menu into the issue, which is the ground truth the importer failed to
                                        find by itself
    outcome                             menu_read | menu_read_partial | web_search_only | none | reply_unreadable | incomplete
    source                              the source that supplied the prices (or the last one tried)
    defaulted                           categories filled with stand-in numbers
    pricing                             the category lists that were read, when there were any
    final                               the five numbers saved on the map
    steps                               every attempt in order: {step, result, url, host, detail, n, platform, chars}

Steps and results are short fixed words so they can be counted. A result of "ok" is a success; anything else is a reason:
    pasted_text   binary | too_few_prices | gemini_error | no_casual_main | ok
    page_html     too_few_prices | gemini_error | no_casual_main | ok        (a page read with a plain request)
    page_browser  too_few_prices | gemini_error | no_casual_main | ok        (a page read with a headless browser)
    fetch         http_<code> | tls_failed | connection_failed | fetch_error | skipped_tls_host | not_a_page
    browser       unavailable | limit | failed | ok
    pdf           download_or_parse_error | no_prices | too_few_prices | ok  (PDF or image menus)
    menu_link     google_search_url
    site_pages    no_menu_page_found | ok                                    (the restaurant's own site; n = pages tried)
    page          load_more | no_prices_links_followed                       (observations about a page, not failures)
    site_gemini   too_few_prices | lunch_prices_suspected | error | ok
    search_gemini error | no_sources | ok
    search_claude no_key | error | refused | no_prices | ok
Add a new word where the importer learns a new way to fail; the report counts whatever it finds.

Nothing here raises: a logging problem must never stop a venue from being added. Menu text itself is not stored, only
counts and the prices read. Add it later if sample pages are wanted for testing a new importer."""
import atexit
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections import Counter, defaultdict

BRANCH = "menu-log"
DATA_FILE = "menu-log.jsonl"
VERSION = 1

# Outcomes that count against a site in the report. menu_read_partial priced the main course but had to invent other categories.
PROBLEM_OUTCOMES = {"none", "web_search_only", "menu_read_partial", "reply_unreadable", "incomplete"}
# Outcomes that leave the venue without a real menu on the map. A later menu_read for the same venue resolves it.
UNRESOLVED_OUTCOMES = {"none", "web_search_only", "reply_unreadable", "incomplete"}

# Hints about what builds a menu page; many failures turn out to share a platform, which is what a better importer would target
PLATFORMS = (
    ("toast", ("toasttab.com", "toast.app", "toasttab")), ("popmenu", ("popmenu.com", "popmenu")),
    ("squarespace", ("squarespace.com", "static1.squarespace", "squarespace")), ("wix", ("wixsite.com", "wixstatic.com", "wix.com", "parastorage.com")),
    ("wordpress", ("wp-content", "wp-includes")), ("bentobox", ("getbento.com", "bentobox")), ("owner", ("owner.com",)),
    ("chownow", ("chownow.com",)), ("olo", ("olo.com",)), ("singleplatform", ("singleplatform.com", "singleplatform")),
    ("spoton", ("spoton.com",)), ("clover", ("clover.com",)), ("square", ("square.site", "squareup.com", "squarespace-cdn")),
    ("menufy", ("menufy.com",)), ("doordash", ("doordash.com",)), ("opentable", ("opentable.com",)), ("resy", ("resy.com",)),
    ("gloriafood", ("gloriafood", "foodbooking.com")), ("webflow", ("webflow.com", "webflow.io")), ("duda", ("dudaone.com", "multiscreensite.com")),
)

_cur = None  # the record being built, or None when no venue is being priced


def log_path():
    return os.environ.get("MENU_LOG") or os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "menu-log.jsonl")


def host_of(url):
    try:
        return urllib.parse.urlparse(url or "").netloc.lower().removeprefix("www.") or None
    except Exception:
        return None


def platform_of(url="", html=""):
    """A short name for the site builder or ordering platform behind a page, or None. A guess from the URL and the start of the HTML."""
    haystack = ((url or "") + " " + (html or "")[:200000]).lower()
    for name, needles in PLATFORMS:
        if any(n in haystack for n in needles):
            return name
    return None


def _scrub(text, limit=200):
    """Short, single-line, and free of anything that looks like a key or a token."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"(?i)(key|token|authorization)=\S+", r"\1=***", text)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9_\-]{20,}", "***", text)
    return text[:limit]


def active():
    return _cur is not None


def start(**context):
    """Begin the record for one venue. Steps logged before this are ignored, so fetches made while checking a website's
    identity do not look like menu failures."""
    global _cur
    try:
        _cur = {"context": context, "steps": [], "t0": time.time()}
    except Exception:
        _cur = None


def step(step, result, url=None, detail=None, **extra):
    """Note one attempt. Does nothing outside start()/finish()."""
    if _cur is None:
        return
    try:
        entry = {"step": step, "result": result}
        if url:
            entry["url"] = _scrub(url, 300)
            entry["host"] = host_of(url)
        if detail:
            entry["detail"] = _scrub(detail)
        entry.update({k: v for k, v in extra.items() if v is not None})
        _cur["steps"].append(entry)
    except Exception:
        pass


def finish(outcome, **fields):
    """Write the record. Safe to call when nothing was started."""
    global _cur
    if _cur is None:
        return
    record, _cur = _cur, None
    try:
        context = record["context"]
        website = context.get("website") or ""
        line = {
            "v": VERSION, "t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "run": os.environ.get("GITHUB_RUN_ID", ""), "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
            "issue": os.environ.get("ISSUE_NUMBER") or None,
            "venue": context.get("venue"), "website": website or None, "host": host_of(website),
            "website_status": context.get("website_status"), "menu_url": context.get("menu_url") or None,
            "reply": bool(context.get("reply")), "outcome": outcome,
            "seconds": round(time.time() - record["t0"], 1),
            **{k: v for k, v in fields.items() if v not in (None, "", [], {})},
            "steps": record["steps"],
        }
        with open(log_path(), "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Could not log the menu import: {e}")


@atexit.register
def _unfinished():
    """A run that ended mid-search (a crash, an abort) still leaves a record."""
    if _cur is not None:
        finish("incomplete")


# ---------------------------------------------------------------- saving to the menu-log branch

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def publish():
    """Append this run's lines to menu-log.jsonl on the menu-log branch (created on first use)."""
    path = log_path()
    if not os.path.exists(path) or not os.path.getsize(path):
        print("No menu imports to log.")
        return
    with open(path) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    ident = ["-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com"]
    work = os.path.join(tempfile.mkdtemp(), "menulog")
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
        message = f"Log {len(lines)} menu import(s) from run {os.environ.get('GITHUB_RUN_ID', 'local')}"
        if git(*ident, "commit", "--quiet", "-m", message, cwd=work).returncode == 0:
            pushed = git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=work)
            if pushed.returncode == 0:
                os.remove(path)
                git("worktree", "remove", "--force", work)
                print(f"Logged {len(lines)} menu import(s) to the {BRANCH} branch.")
                return
            print(f"Push rejected (attempt {attempt}): {pushed.stderr.strip()[-200:]}")
        git("worktree", "remove", "--force", work)
    print("Could not save the menu log; this run's menu imports are not recorded.")


def load_entries():
    """Every logged record, read from the menu-log branch on origin."""
    if git("fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}").returncode:
        return []
    entries = []
    for line in git("show", f"origin/{BRANCH}:{DATA_FILE}").stdout.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


# ---------------------------------------------------------------- the report

def _failures(entry):
    """(step, result) for every attempt in a record that did not succeed or merely note something."""
    return [(s["step"], s["result"]) for s in entry.get("steps", []) if s.get("result") not in ("ok",)]


def report(entries, recent=15):
    if not entries:
        return "Menu import log: nothing logged yet."
    entries = sorted(entries, key=lambda e: e.get("t", ""))
    out = [f"Menu import log: {len(entries)} import(s) from {entries[0]['t'][:10]} to {entries[-1]['t'][:10]}", ""]

    outcomes = Counter(e.get("outcome") for e in entries)
    out.append("Outcomes")
    for name, n in outcomes.most_common():
        out.append(f"  {name}: {n} ({round(100 * n / len(entries))}%)")

    reasons = Counter(r for e in entries for r in _failures(e))
    out += ["", "Failure reasons, most common first (step: result)"]
    for (step, result), n in reasons.most_common(15):
        out.append(f"  {n:>4}  {step}: {result}")
    if not reasons:
        out.append("  (none)")

    # A site is a problem when a fetch of it failed or when a venue on it ended without a full menu
    hosts = defaultdict(lambda: {"venues": set(), "reasons": Counter(), "problem_imports": 0, "imports": 0, "platform": Counter(), "last": ""})
    for e in entries:
        seen = set()
        for s in e.get("steps", []):
            h = s.get("host")
            if not h:
                continue
            row = hosts[h]
            row["venues"].add(e.get("venue"))
            row["last"] = max(row["last"], e["t"])
            if s.get("platform"):
                row["platform"][s["platform"]] += 1
            if s.get("result") not in ("ok", None):
                row["reasons"][f"{s['step']}:{s['result']}"] += 1
            seen.add(h)
        own = e.get("host")
        if own:
            seen.add(own)
            row = hosts[own]
            row["venues"].add(e.get("venue"))
            row["last"] = max(row["last"], e["t"])
        for h in seen:
            hosts[h]["imports"] += 1
            if e.get("outcome") in PROBLEM_OUTCOMES:
                hosts[h]["problem_imports"] += 1
    bad = [(h, r) for h, r in hosts.items() if r["reasons"] or r["problem_imports"]]
    bad.sort(key=lambda hr: (-hr[1]["problem_imports"], -sum(hr[1]["reasons"].values())))
    out += ["", "Problem sites (restaurants' own sites and menu pages that failed to read or left a venue without a full menu)"]
    for h, r in bad[:20]:
        platform = r["platform"].most_common(1)[0][0] if r["platform"] else "?"
        why = ", ".join(f"{k} x{n}" for k, n in r["reasons"].most_common(3)) or "no fetch error, but no full menu"
        out.append(f"  {h}  [{platform}]  {r['problem_imports']}/{r['imports']} imports ended badly  ({why})  last {r['last'][:10]}")
    if not bad:
        out.append("  (none)")

    platforms = Counter()
    for h, r in bad:
        if r["platform"]:
            platforms[r["platform"].most_common(1)[0][0]] += 1
    if platforms:
        out += ["", "Platforms behind the problem sites: " + ", ".join(f"{k} {n}" for k, n in platforms.most_common())]

    latest = {}
    for e in entries:
        latest[(e.get("venue") or "").lower()] = e
    unresolved = [e for e in latest.values() if e.get("outcome") in UNRESOLVED_OUTCOMES]
    out += ["", f"Venues whose latest import has no real menu ({len(unresolved)})"]
    for e in sorted(unresolved, key=lambda e: e["t"], reverse=True):
        out.append(f"  {e['venue']}  {e.get('host') or 'no website'}  {e['outcome']}  {e['t'][:10]}")
    if not unresolved:
        out.append("  (none)")

    replies = [e for e in entries if e.get("reply") and e.get("outcome") == "menu_read"]
    if replies:
        out += ["", f"Menus someone had to paste in ({len(replies)}); each is a case the importer could not do alone"]
        for e in replies[-recent:]:
            out.append(f"  {e['venue']}  {e.get('host') or 'no website'}  {e['t'][:10]}")

    out += ["", f"Recent imports (newest first, {recent} shown)"]
    for e in entries[::-1][:recent]:
        src = f"{e['workflow']} run {e['run']}" if e.get("run") else e["workflow"]
        out.append(f"  {e['t'][:16].replace('T', ' ')}  {e['venue']}  {e['outcome']}  via {e.get('source') or '-'}  [{src}]")
    return "\n".join(out)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "publish":
        publish()
    elif command == "report":
        recent = int(sys.argv[sys.argv.index("--recent") + 1]) if "--recent" in sys.argv else 15
        print(report(load_entries(), recent))
    elif command == "export":
        for e in load_entries():
            print(json.dumps(e, ensure_ascii=False))
    else:
        sys.exit("usage: menu_log.py [report [--recent N] | export | publish]")


if __name__ == "__main__":
    main()
