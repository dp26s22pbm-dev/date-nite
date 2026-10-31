"""Find date-night restaurants inside grid cells (see grid_cells.py and datenite.us/grid/): search Google Places in
each cell's rectangle, rank the places the same way add_city.py does, and add the best ones with the importer until the
cell holds fullAt venues (5) or the candidates run out. A cell that already holds fullAt venues is reported as full and
not searched, which is the cue to split it. Every cell that is searched is added to the "searched" list in
grid/grid.json, so the grid page can tell a cell with no qualifying restaurants from one nobody has looked at.

Cells come from the Search Grid Cells form (ISSUE_BODY, the lines under its "Cells to Search" heading). As with cities,
the new venues are saved without ids to pending-venues/cells-<issue>.json; the workflow (add-cell.yml) commits that
file and grid.json to a branch and opens a pull request, and fold_pending_venues.py numbers them after the merge."""
import os
import re
import json
import tempfile

import add_city as ac
import places_usage  # noqa: F401  (add_city records every Places request through it)
from grid_cells import load_grid, save_grid, parse_cell, cell_bounds, cell_of, cell_id, sort_key
from split_cells import parse_field, set_output

MAX_CELLS = int(os.environ.get("MAX_CELLS", "5"))
PR_TITLE_PREFIX = "Add venues for cells: "

def open_cell_prs():
    """{cell id: pull request url} for open pull requests this workflow made (titles "Add venues for cells: G4, G5 (#12)").
    Their venues are not on main yet, so a second run on the same cell would price the same places again. Returns {}
    when gh is unavailable or fails, so a lookup problem never blocks a run."""
    import subprocess
    try:
        out = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "url,title"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return {}
        found = {}
        for pr in json.loads(out.stdout or "[]"):
            title = pr.get("title", "")
            if title.startswith(PR_TITLE_PREFIX):
                for token in re.split(r"[\s,]+", title[len(PR_TITLE_PREFIX):].split(" (#")[0].upper()):
                    found.setdefault(token, pr["url"])
        return found
    except Exception:
        return {}

def load_exclusions():
    """[(name_key prefix, city or None)] from excluded-venues.json."""
    if not os.path.exists(ac.EXCLUDED_FILE):
        return []
    with open(ac.EXCLUDED_FILE) as f:
        return [(ac.name_key(e["name"]), (e.get("city") or "").strip().lower() or None)
                for e in json.load(f) if ac.name_key(e["name"])]

def excluded_here(place, exclusions):
    """True when an excluded-venues.json entry covers the place: no city on the entry, or the city is in its address."""
    key = ac.name_key((place.get("displayName") or {}).get("text", ""))
    address = (place.get("formattedAddress") or "").lower()
    return any(key.startswith(prefix) and (city is None or city in address) for prefix, city in exclusions)

def main():
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GOOGLE_PLACES_API_KEY is not set")
    grid = load_grid()
    splits, skip, full_at = set(grid.get("split", [])), set(grid.get("skip", [])), grid["fullAt"]
    tokens = parse_field(os.environ.get("ISSUE_BODY", ""), "Cells to Search")

    cells, notes = [], []  # the cells to search, and a line for each id that cannot be
    open_prs = open_cell_prs()
    for token in tokens:
        base, child, error = parse_cell(grid, token)
        if error:
            notes.append(f"- {error}")
        elif base in skip:
            notes.append(f"- {token}: {base} is marked as water and has no restaurants to find")
        elif child is None and base in splits:
            notes.append(f"- {token}: {base} is split; search its cells ({base}-a to {base}-{chr(96 + grid['splitInto'] ** 2)})")
        elif child is not None and base not in splits:
            notes.append(f"- {token}: {base} is not split; search {base}, or split it first")
        elif cell_id(base, child) in open_prs:
            notes.append(f"- {token}: a pull request for it is already open ({open_prs[cell_id(base, child)]}). "
                         "Merge or close it, then search the cell again")
        elif cell_id(base, child) not in [c[0] for c in cells]:
            cells.append((cell_id(base, child), base, child))
    for cid, _, _ in cells[MAX_CELLS:]:
        notes.append(f"- {cid}: not run, at most {MAX_CELLS} cells go in one issue. Open another issue for it")
    cells = cells[:MAX_CELLS]

    saved = json.load(open("venues.json")) + ac.load_pending()  # pending: merged cell and city venues not folded in yet
    saved_names = {ac.name_key(v["name"]) for v in saved}
    exclusions = load_exclusions()

    sections, searched, added_total, priced_total = [], [], [], 0
    with tempfile.TemporaryDirectory() as work_dir:
        # The importer numbers and checks new venues against this scratch copy; venues.json itself stays untouched
        venues_file = os.path.join(work_dir, "venues.json")
        with open(venues_file, "w") as f:
            json.dump(saved, f)
        for cid, base, child in cells:
            have = sum(1 for v in saved if cell_of(grid, v["coords"], splits) == cid)
            have += sum(1 for v in added_total if cell_of(grid, v["coords"], splits) == cid)
            if have >= full_at:
                hint = f"Split {base} to search it in smaller cells." if child is None else "It is already the smallest cell."
                sections.append(f"## {cid}\n\nAlready holds {have} venues (a cell holds {full_at}), so it was not searched. {hint}")
                continue
            south, west, north, east = cell_bounds(grid, base, child)
            places = ac.search_city("", api_key, rectangle=(south, west, north, east))
            searched.append(cid)
            inside = [p for p in places
                      if cell_of(grid, [p["location"]["longitude"], p["location"]["latitude"]], splits) == cid]
            candidates = [p for p in inside
                          if not excluded_here(p, exclusions) and ac.eligible(p, "", saved_names)]
            ranked = ac.rank(candidates)
            print(f"\n{cid}: {len(places)} places found, {len(inside)} inside the cell, {len(candidates)} eligible")
            if not ranked:
                sections.append(f"## {cid}\n\nNo restaurants qualified (open, with at least {ac.MIN_REVIEWS} reviews, "
                                "not a chain or counter-service place, and not already on the map).")
                continue
            before = len(json.load(open(venues_file)))
            cell_sections, skipped, kept, priced = ac.price_and_keep(
                ranked, work_dir, venues_file, count=full_at - have, number_start=priced_total)
            priced_total += priced
            new = json.load(open(venues_file))[before:]
            for v in new:
                saved_names.add(ac.name_key(v["name"]))
            added_total += new
            tail = ("\n\n### Skipped\n\n" + "\n".join(skipped)) if skipped else ""
            body = "\n\n".join(cell_sections) if cell_sections else "No candidate could be added."
            sections.append(f"## {cid}: {kept} added ({have + kept} of {full_at})\n\n{body}{tail}")
        added = json.load(open(venues_file))[len(saved):]
    for venue in added:
        venue.pop("id", None)  # ids are handed out when the pull request merges (fold_pending_venues.py)

    pending_file = ""
    if added:
        os.makedirs(ac.PENDING_DIR, exist_ok=True)
        pending_file = os.path.join(ac.PENDING_DIR, f"cells-{os.environ.get('ISSUE_NUMBER', '0')}.json")
        with open(pending_file, "w") as f:
            json.dump(added, f, indent=2)
            f.write("\n")
    marked = sorted(set(grid.get("searched", [])) | set(searched), key=sort_key)
    if marked != grid.get("searched", []):
        grid["searched"] = marked
        save_grid(grid)
    set_output("pending_file", pending_file)
    set_output("count", len(added))
    set_output("title", PR_TITLE_PREFIX + ", ".join(searched))  # only searched cells, so a full cell never blocks a later run
    set_output("changed", "true" if added or searched else "false")  # the workflow opens a pull request only then

    if not cells and not notes:
        intro = "No cells were given."
    else:
        intro = (f"Searched {', '.join(searched) or 'no cells'} for restaurants, up to {full_at} venues per cell, ranked by "
                 f"Google rating weighted by review count (at least {ac.MIN_REVIEWS} reviews, at most {ac.MAX_PER_CUISINE} "
                 f"of one cuisine, no national fast-food chains, nothing under ${ac.MIN_TOTAL_FOR_TWO} for two, no venue "
                 "without any prices found, no counter-service places). Ratings were used only for the ranking and are not saved.\n\n"
                 f"Review the prices and pins below before merging. The venues are in `{pending_file or 'no file (none added)'}`; "
                 "delete an entry there to drop it. They get their ids and join `venues.json` automatically after the merge. "
                 "Every searched cell is also recorded in `grid/grid.json`, so the grid page shows it as searched.")
    notes_text = ("\n\n## Not searched\n\n" + "\n".join(notes)) if notes else ""
    report = intro + "\n\n" + "\n\n".join(sections) + notes_text
    print(report)
    set_output("report", report, multiline=True)

if __name__ == "__main__":
    main()
