"""Per-venue Approve / Deny checkboxes on a Search Grid Cells pull request, and the review issues for undecided venues.

search-cells.yml saves the candidate venues in venue-review/cells-<issue>.json and puts a "Decisions" block (see
decision_block) at the bottom of the pull request description, two task-list items per venue. After the pull request
merges, the venue-review workflow runs `merge`:
  - Approve ticked        -> the venue is folded into venues.json (fold_pending_venues.py gives it an id)
  - Deny ticked           -> dropped
  - neither (or both)     -> held: a `venue-review` issue is opened with the venue's data
Adding the `approve-venue` label to such an issue runs `approve`, which folds that venue in; closing the issue without
the label leaves the venue off the map.

Pasting a menu into a comment on such an issue runs `menu`, which reads the prices from it and rewrites the data block
in the issue body (the venue is still added only when the label is applied).

Usage: python venue_review.py merge | issues | approve | menu
  merge:   PR_BODY                               (reads venue-review/cells-<N>.json, N from "Closes #N" in PR_BODY)
  issues:  PR_URL, PR_NUMBER                     (opens an issue for each venue merge left on hold)
  approve: ISSUE_BODY, ISSUE_NUMBER
  menu:    ISSUE_BODY, ISSUE_NUMBER, COMMENT_BODY, GEMINI_API_KEY  (a comment of `app: 17.99`-style lines sets prices directly)"""
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

def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            one_line = re.sub(r"[\r\n]+", " ", str(value))  # outside the f-string: Python 3.11 forbids backslashes inside one
            f.write(f"{key}={one_line}\n")

PRICE_KEYS = ("app", "casualMain", "premiumMain", "drink", "dessert")
OVERRIDE_LINE = re.compile(r"^\s*(app|casualMain|premiumMain|drink|dessert)\s*:\s*\$?\s*(\d+(?:\.\d+)?)\s*$", re.I)

def parse_price_overrides(text):
    """Return {key: price} when every non-blank line of the comment is a `key: price` line, else {} (a pasted menu)."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    found = {}
    for line in lines:
        m = OVERRIDE_LINE.match(line)
        if not m:
            return {}
        key = next(k for k in PRICE_KEYS if k.lower() == m.group(1).lower())
        found[key] = float(m.group(2))
    return found

def menu():
    """Read prices from a menu pasted into a comment and write them into the venue data block of the issue body.
    The venue is not added to the map; add the `approve-venue` label afterward to do that."""
    # Imported here because it creates a Gemini client on import and the other commands do not need one
    import statistics
    import add_venue_from_issue as add
    import pricing_rules

    number = os.environ["ISSUE_NUMBER"]
    body = os.environ["ISSUE_BODY"]
    comment = os.environ.get("COMMENT_BODY", "")
    venue = parse_issue_venue(body)
    overrides = parse_price_overrides(comment)
    defaulted = []
    if overrides:
        # A comment of "app: 17.99" style lines sets those prices directly; any price left out keeps its saved value
        merged = {k: overrides.get(k, venue.get(k)) for k in PRICE_KEYS}
        missing = [k for k in PRICE_KEYS if merged[k] is None]
        if missing:
            set_output("error", "Those prices leave out " + ", ".join(missing) + ", and the venue has no saved value for "
                                "it. Include all five: app, casualMain, premiumMain, drink, dessert.")
            sys.exit(1)
        app, casual, premium, drink, dessert = (merged[k] for k in PRICE_KEYS)
        source = "the prices you gave"
    else:
        pricing = add.pricing_from_menu_text(comment)
        if not pricing:
            set_output("error", "I could not read prices from that text. Reply with the full menu page (open it, select all, "
                                "copy, paste); it needs at least a handful of dish prices like $12.99. Or reply with lines "
                                "like `app: 17.99`.")
            sys.exit(1)
        casual = round(statistics.median(pricing["casualMains"]), 2)
        defaulted = [label for key, label in (("premiumMains", "premium main"), ("apps", "appetizer"), ("drinks", "drink"),
                                              ("desserts", "dessert")) if not pricing.get(key)]
        premium = round(statistics.median(pricing.get("premiumMains") or [round(casual * 1.6, 2)]), 2)
        app = round(statistics.median(pricing.get("apps") or [round(casual * 0.55, 2)]), 2)
        drink = round(statistics.median(pricing.get("drinks") or [max(8.00, round(casual * 0.5, 2))]), 2)
        dessert = round(statistics.median(pricing.get("desserts") or [8.00]), 2)
        source = "your menu"

    is_taco = pricing_rules.is_taco_place(venue.get("name", ""), venue.get("category", "")) \
        and pricing_rules.per_taco_prices(app, casual)
    total = round((app + (2 if is_taco else 1) * casual + premium + 2 * drink + dessert) * 1.30)
    venue.update({"app": app, "casualMain": casual, "premiumMain": premium, "drink": drink, "dessert": dessert,
                  "tierColor": add.calculate_tier_color(total)})
    if is_taco:
        venue["isTaco"] = True
    else:
        venue.pop("isTaco", None)

    m = re.match(r"Held from (.*?) for later review\.", body)
    gh("issue", "edit", number, "--body", issue_body(venue, m.group(1) if m else "an earlier search"))

    summary = (f"Prices updated from {source}: appetizer ${app:g}, casual main ${casual:g}, premium main ${premium:g}, "
               f"drink ${drink:g}, dessert ${dessert:g} (about ${total} for two).")
    if defaulted:
        summary += " No menu price was found for the " + ", ".join(defaulted) + ", so a typical stand-in was used."
    if premium < casual:
        summary += (f" Check the tiers: the premium main (${premium:g}) is lower than the casual main (${casual:g}), so the "
                    "dishes may have been sorted wrongly. Reply with lines like `casualMain: 27.70` to correct it.")
    summary += f" Add the `{APPROVE_LABEL}` label to put the venue on the map."
    set_output("summary", summary)
    print(summary)

if __name__ == "__main__":
    {"merge": merge, "issues": issues, "approve": approve, "menu": menu}[sys.argv[1]]()
