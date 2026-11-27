"""Decide Approve / Deny / Hold for the venues a daily grid search found, and optionally apply the decisions.

    python triage.py decide     classify the run's candidates; write the state, the summary and the (possibly ticked) report
    python triage.py apply      apply the state written by `decide` to the checkout (the workflow runs it after a reset to main)

The rules are deliberately narrow. Approve is for a venue whose prices were read from the restaurant's own menu with
nothing invented and nothing flagged. Deny is for a place that is plainly a venue already on the map. Everything else
is held, which is what happens today to a venue nobody ticked.

Approve needs every one of:
  - a menu-log record for the venue from this run, with outcome menu_read (all five categories read, none defaulted)
  - the prices came from the restaurant's own site, a menu link or a menu PDF (not a web search, the Maps Menu tab or an estimate)
  - no price sanity warning (the venue has no reviewNotes), no `prices` note in the log (appetizers above the mains, a premium
    tier that repeats the casual mains), no implausibly low site read, and no Gemini spending-cap or run-budget stop
  - a town and state
  - no venue within NEAR_M metres with a similar name or the same website, on the map or among this run's other candidates
  - fewer than AUTO_APPROVE_MAX approvals already given in the run (the rest are held)
Deny: a venue within NEAR_M metres of one on the map that has the same website host, which is the same business under another name.

AUTO_TRIAGE (repository variable, read by daily-search.yml):
  off      the pull request is made as before, every box empty
  suggest  the pull request's boxes are ticked to match, and a table gives the reason for each; a person still merges (default)
  apply    the daily run folds approved venues into main itself, adds denied ones to excluded-venues.json, opens a
           venue-review issue for each held venue, and posts the table on the run's issue so a decision can be undone

Environment: AUTO_TRIAGE, AUTO_APPROVE_MAX (default 10), ISSUE_NUMBER, REPORT (add_cell.py's report), RUNNER_TEMP,
MENU_LOG (the run's menu log file; defaults the same way the importer's does).
Outputs: mode, apply (true when the workflow should apply instead of opening a pull request), approved, denied, held."""
import os
import re
import sys
import json
import math
import difflib
import tempfile
from urllib.parse import urlparse

import menu_log
import venue_review
import fold_pending_venues as fold
from grid_cells import load_grid, save_grid, sort_key
from split_cells import set_output

NEAR_M = 150
OWN_MENU_SOURCES = ("the menu on the restaurant's website", "Gemini reading the restaurant's own menu pages",
                    "the menu PDF", "the menu link")
MODES = ("off", "suggest", "apply")


def tmp(name):
    return os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), name)


STATE_FILE, SUMMARY_FILE, REPORT_FILE = tmp("triage-state.json"), tmp("triage-summary.md"), tmp("triage-report.md")


def mode():
    value = (os.environ.get("AUTO_TRIAGE") or "suggest").strip().lower()
    return value if value in MODES else "suggest"


def name_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def host_of(url):
    host = urlparse(url if "//" in (url or "") else f"//{url or ''}").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def distance_m(a, b):
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def similar_names(a, b):
    a, b = name_key(a), name_key(b)
    if not a or not b:
        return False
    if a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def local_records(path=None):
    """{name key: latest menu-log record} from the run's own log file."""
    records = {}
    try:
        with open(path or menu_log.log_path()) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                records[name_key(r.get("venue"))] = r
    except OSError:
        pass
    return records


def record_problems(record):
    """Reasons a menu-log record does not show a clean read of the restaurant's own menu. [] when it does."""
    if record is None:
        return ["no menu-log record for this venue in this run"]
    problems = []
    if record.get("outcome") != "menu_read":
        problems.append(f"the menu read ended as {record.get('outcome') or 'unknown'}, not a full read")
    if record.get("defaulted"):
        problems.append("stand-in prices were used for " + ", ".join(map(str, record["defaulted"])))
    source = str(record.get("source") or "")
    if not source.startswith(OWN_MENU_SOURCES):
        problems.append(f"prices came from {source or 'an unknown source'}, not the restaurant's own menu")
    for s in record.get("steps") or []:
        if s.get("step") == "prices":
            problems.append(f"price check noted {s.get('result')}")
        elif s.get("step") == "site_pages" and s.get("result") == "implausibly_low":
            problems.append("the site's prices looked implausibly low")
        elif s.get("step") == "gemini" and s.get("result") in ("cap_reached", "run_budget_reached"):
            problems.append("Gemini stopped during the read (spending cap or run budget)")
    return problems


def duplicate_of(venue, others):
    """(other venue, same_business) for the first nearby venue that looks like the same place, else (None, False).
    same_business is True when the website host matches too, which is how a rename shows up."""
    for other in others:
        if not (venue.get("coords") and other.get("coords")) or distance_m(venue["coords"], other["coords"]) > NEAR_M:
            continue
        host, other_host = host_of(venue.get("website")), host_of(other.get("website"))
        same_site = bool(host) and host == other_host
        if same_site or similar_names(venue.get("name"), other.get("name")):
            return other, same_site
    return None, False


def classify(venues, records, on_map, max_approve=10):
    """[(decision, [reasons])] for each venue, in order. `on_map` is every venue already saved or pending."""
    results, approved_so_far = [], []
    for v in venues:
        dup, same_site = duplicate_of(v, on_map)
        if dup is not None and same_site:
            results.append(("deny", [f"same website and within {NEAR_M} m of {dup['name']}, which is already on the map"]))
            continue
        reasons = record_problems(records.get(name_key(v.get("name"))))
        if v.get("reviewNotes"):
            reasons += [f"flagged: {n}" for n in v["reviewNotes"]]
        if not (v.get("city") and v.get("state")):
            reasons.append("no town or state")
        if dup is not None:
            reasons.append(f"possible duplicate of {dup['name']}, {NEAR_M} m or less away")
        twin, _ = duplicate_of(v, approved_so_far)
        if twin is not None:
            reasons.append(f"looks like {twin['name']}, approved in this same run")
        if not reasons and len(approved_so_far) >= max_approve:
            reasons.append(f"already approved {max_approve} venues this run")
        if reasons:
            results.append(("hold", reasons))
        else:
            approved_so_far.append(v)
            results.append(("approve", ["prices read from the restaurant's own menu, nothing flagged, no duplicate nearby"]))
    return results


def tick_report(report, decisions):
    """The pull request report with each venue's Approve or Deny box ticked. Holds stay empty."""
    for i, (decision, _) in enumerate(decisions):
        if decision in ("approve", "deny"):
            kind = decision.capitalize()
            report = re.sub(rf"(- \[) (\] {kind} <!-- v{i} -->)", r"\1x\2", report)
    return report


def table(venues, decisions, applied):
    label = {"approve": "Approved" if applied else "Approve", "deny": "Denied" if applied else "Deny", "hold": "Held" if applied else "Hold"}
    rows = ["| Venue | Decision | Why |", "| --- | --- | --- |"]
    for v, (decision, reasons) in zip(venues, decisions):
        name = v['name'].replace('|', '/')  # a pipe in a venue name ("Traverso's Restaurant | Italian") would split the row
        rows.append(f"| {name} | {label[decision]} | {'; '.join(reasons).replace('|', '/')} |")
    return "\n".join(rows)


UNDO = ("**To undo a decision**\n\n"
        "- An approved venue is on the map now. Take it off with the Remove a Venue issue form (name, town and state).\n"
        "- A denied venue is in `excluded-venues.json`. Delete its entry there and search its cell again to bring it back.\n"
        "- A held venue has its own `venue-review` issue. Add the `approve-venue` label there to put it on the map, or "
        "`deny-venue` to blacklist it.")


def summary(venues, decisions, mode_name, searched):
    counts = {k: sum(1 for d, _ in decisions if d == k) for k in ("approve", "deny", "hold")}
    applied = mode_name == "apply"
    if mode_name == "off":
        return f"Auto triage is off: {len(venues)} venue(s) from {', '.join(searched) or 'no cells'} are left for review."
    head = (f"{'Applied' if applied else 'Suggested'} for {len(venues)} venue(s) from {', '.join(searched) or 'no cells'}: "
            f"{counts['approve']} approve, {counts['deny']} deny, {counts['hold']} hold.")
    if not venues:
        return head
    body = [head, "", table(venues, decisions, applied)]
    if applied:
        body += ["", UNDO]
    else:
        body += ["", "The Approve and Deny boxes above are ticked to match. Change any of them before you merge."]
    return "\n".join(body)


def newly_searched(grid, key="searched"):
    """Cells this run added to a grid.json list ("searched", or "split" for cells add_cell.py split on its own): the working
    copy against the committed one. [] when git cannot say."""
    import subprocess
    try:
        out = subprocess.run(["git", "show", "HEAD:grid/grid.json"], capture_output=True, text=True, timeout=30)
        before = set(json.loads(out.stdout).get(key, [])) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        before = None
    if before is None:
        return []
    return sorted(set(grid.get(key, [])) - before, key=sort_key)


def current_candidates(issue):
    path = os.path.join(venue_review.REVIEW_DIR, f"cells-{issue}.json")
    if not os.path.exists(path):
        return path, []
    with open(path) as f:
        return path, json.load(f)


def decide():
    mode_name = mode()
    issue = os.environ.get("ISSUE_NUMBER", "0")
    report = os.environ.get("REPORT", "")
    path, venues = current_candidates(issue)
    grid = load_grid()
    with open("venues.json") as f:
        on_map = json.load(f)
    on_map += fold_pending()
    try:
        limit = int(os.environ.get("AUTO_APPROVE_MAX") or 10)
    except ValueError:
        limit = 10
    decisions = classify(venues, local_records(), on_map, limit) if mode_name != "off" else [("hold", ["triage is off"]) for _ in venues]
    searched = newly_searched(grid)
    if mode_name == "suggest":
        report = tick_report(report, decisions)
        if venues:
            report += "\n\n## Triage\n\n" + table(venues, decisions, False) + "\n\nThe boxes above are ticked to match. Change any of them before you merge."
    state = {"issue": issue, "mode": mode_name, "venues": venues, "decisions": [d for d, _ in decisions],
             "reasons": [r for _, r in decisions], "searched": searched, "split": newly_searched(grid, "split")}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    with open(REPORT_FILE, "w") as f:
        f.write(report)
    text = summary(venues, decisions, mode_name, searched)
    if state["split"]:
        text += f"\n\nSplit automatically because they held more prospects than a cell takes: {', '.join(state['split'])}."
    with open(SUMMARY_FILE, "w") as f:
        f.write(text)
    print(text)
    counts = {k: sum(1 for d, _ in decisions if d == k) for k in ("approve", "deny", "hold")}
    set_output("mode", mode_name)
    set_output("apply", "true" if mode_name == "apply" else "false")
    for k, name in (("approve", "approved"), ("deny", "denied"), ("hold", "held")):
        set_output(name, counts[k])


def fold_pending():
    out = []
    if os.path.isdir(fold.PENDING_DIR):
        for name in sorted(os.listdir(fold.PENDING_DIR)):
            if name.endswith(".json"):
                with open(os.path.join(fold.PENDING_DIR, name)) as f:
                    out += json.load(f)
    return out


def apply():
    """Fold the approved venues, blacklist the denied ones, mark the searched cells and list the held venues for issues().
    Safe to run again on a fresh checkout after a rejected push: a venue already on the map or already excluded is skipped."""
    with open(STATE_FILE) as f:
        state = json.load(f)
    venues, decisions = state["venues"], state["decisions"]
    approved = [v for v, d in zip(venues, decisions) if d == "approve"]
    denied = [v for v, d in zip(venues, decisions) if d == "deny"]
    held = [v for v, d in zip(venues, decisions) if d == "hold"]
    if approved:
        venue_review.write_pending(f"cells-{state['issue']}.json", approved)
        fold.main()
    venue_review.exclude(denied, "denied automatically: same business as a venue on the map")
    grid = load_grid()
    marked = sorted(set(grid.get("searched", [])) | set(state.get("searched", [])), key=sort_key)
    if marked != grid.get("searched", []):
        grid["searched"] = marked
        save_grid(grid)
    new_split = [c for c in state.get("split", []) if c not in grid.get("split", [])]
    if new_split:
        grid["split"] = sorted(set(grid.get("split", [])) | set(new_split), key=sort_key)
        save_grid(grid)
    with open(venue_review.HELD_FILE, "w") as f:
        json.dump(held, f)
    print(f"Applied: {len(approved)} approved, {len(denied)} denied, {len(held)} held.")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "decide":
        decide()
    elif command == "apply":
        apply()
    else:
        sys.exit("usage: triage.py [decide | apply]")


if __name__ == "__main__":
    main()
