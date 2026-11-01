"""Per-venue Approve / Deny checkboxes on a Search Grid Cells pull request, and the review issues for undecided venues.

search-cells.yml saves the candidate venues in venue-review/cells-<issue>.json and puts a "Decisions" block (see
decision_block) at the bottom of the pull request description, two task-list items per venue. After the pull request
merges, the venue-review workflow runs `merge`:
  - Approve ticked        -> the venue is folded into venues.json (fold_pending_venues.py gives it an id)
  - Deny ticked           -> dropped
  - neither (or both)     -> held: a `venue-review` issue is opened with the venue's data
Adding the `approve-venue` label to such an issue runs `approve`, which folds that venue in; closing the issue without
the label leaves the venue off the map.

Usage: python venue_review.py merge | issues | approve
  merge:   PR_BODY                               (reads venue-review/cells-<N>.json, N from "Closes #N" in PR_BODY)
  issues:  PR_URL, PR_NUMBER                     (opens an issue for each venue merge left on hold)
  approve: ISSUE_BODY, ISSUE_NUMBER"""
import os
import re
import sys
import json
import subprocess

import fold_pending_venues as fold

REVIEW_DIR = "venue-review"
LABEL = "venue-review"
APPROVE_LABEL = "approve-venue"
HELD_FILE = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "held-venues.json")
DATA_START = "<!-- venue-data"
DATA_END = "venue-data -->"
BOX = re.compile(r"^\s*[-*] \[([ xX])\] (Approve|Deny) <!-- v(\d+) -->\s*$", re.M)

def decision_block(venues):
    lines = ["## Decisions", "",
             "Tick **Approve** or **Deny** for each venue. Leave both empty to hold a venue: it is not added when you merge, "
             "and gets its own `venue-review` issue where you can approve it later.", ""]
    for i, v in enumerate(venues):
        lines += [f"### {v['name']}", f"- [ ] Approve <!-- v{i} -->", f"- [ ] Deny <!-- v{i} -->", ""]
    return "\n".join(lines).rstrip()

def parse_decisions(body, count):
    """Return a list of 'approve' / 'deny' / 'hold', one per venue. Both boxes ticked, or neither, means hold."""
    ticked = {}
    for mark, kind, idx in BOX.findall(body or ""):
        if mark.lower() == "x":
            ticked.setdefault(int(idx), set()).add(kind.lower())
    out = []
    for i in range(count):
        t = ticked.get(i, set())
        out.append("approve" if t == {"approve"} else "deny" if t == {"deny"} else "hold")
    return out

def issue_body(venue, source_url):
    data = json.dumps(venue, indent=2)
    prices = ", ".join(f"{label} ${venue[k]:g}" for k, label in
                       [("app", "appetizer"), ("casualMain", "casual main"), ("premiumMain", "premium main"),
                        ("drink", "drink"), ("dessert", "dessert")] if k in venue)
    return (f"Held from {source_url} for later review.\n\n"
            f"- **Cuisine:** {venue.get('category', '?')}\n- **Website:** {venue.get('website', 'none')}\n"
            f"- **Prices:** {prices}\n\n"
            f"Add the `{APPROVE_LABEL}` label to put this venue on the map. Close the issue to leave it off. "
            "Fix the prices or cuisine first by editing the JSON below.\n\n"
            f"{DATA_START}\n{data}\n{DATA_END}")

def parse_issue_venue(body):
    m = re.search(re.escape(DATA_START) + r"\s*(.*?)\s*" + re.escape(DATA_END), body or "", re.S)
    if not m:
        raise SystemExit("No venue data block found in the issue.")
    return json.loads(m.group(1))

def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout

def write_pending(name, venues):
    os.makedirs(fold.PENDING_DIR, exist_ok=True)
    with open(os.path.join(fold.PENDING_DIR, name), "w") as f:
        json.dump(venues, f, indent=2)
        f.write("\n")

def merge():
    """Fold the approved venues and delete the review file. Held venues go to HELD_FILE for issues() to open once the
    push has succeeded (the fold is redone from a fresh checkout if the push is rejected, so it must not open issues)."""
    body = os.environ["PR_BODY"]
    m = re.search(r"Closes #(\d+)", body)
    if not m:
        raise SystemExit("No 'Closes #N' in the pull request description.")
    path = os.path.join(REVIEW_DIR, f"cells-{m.group(1)}.json")
    if not os.path.exists(path):
        print(f"{path} not found; nothing to do.")
        return
    with open(path) as f:
        venues = json.load(f)
    decisions = parse_decisions(body, len(venues))
    approved = [v for v, d in zip(venues, decisions) if d == "approve"]
    held = [v for v, d in zip(venues, decisions) if d == "hold"]
    print(f"{len(approved)} approved, {len(held)} held, {decisions.count('deny')} denied.")
    if approved:
        write_pending(f"cells-{m.group(1)}.json", approved)
    os.remove(path)
    fold.main()
    with open(HELD_FILE, "w") as f:
        json.dump(held, f)

def issues():
    if not os.path.exists(HELD_FILE):
        return
    with open(HELD_FILE) as f:
        held = json.load(f)
    pr_url = os.environ.get("PR_URL", f"pull request #{os.environ.get('PR_NUMBER', '?')}")
    for v in held:
        gh("issue", "create", "--title", f"[Venue Review]: {v['name']}", "--label", LABEL,
           "--body", issue_body(v, pr_url))
        print(f"Opened a review issue for {v['name']}.")

def approve():
    venue = parse_issue_venue(os.environ["ISSUE_BODY"])
    write_pending(f"review-{os.environ['ISSUE_NUMBER']}.json", [venue])
    fold.main()

if __name__ == "__main__":
    {"merge": merge, "issues": issues, "approve": approve}[sys.argv[1]]()
