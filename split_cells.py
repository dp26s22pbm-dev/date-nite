"""Change the "split" list in grid/grid.json. Read from the Split Grid Cells form (ISSUE_BODY): the cells under its
"Cells to Split" heading are added, and the cells under "Cells to Merge Back" are removed (their children also leave
the "searched" list). Only base cells (A1, G4, ...) split; a child id such as G4-b is read as its parent G4.
Ids outside the grid, already split (or not split, for a merge), or marked as water are reported and skipped.
Writes nothing when nothing changes.

Outputs for the workflow (GITHUB_OUTPUT): count (cells changed) and report (text for the pull request or issue comment)."""
import os
import re

from grid_cells import load_grid, save_grid, parse_cell, sort_key

def set_output(key, value, multiline=False):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            if multiline:
                f.write(f"{key}<<EOF_{key}\n{value}\nEOF_{key}\n")
            else:
                one_line = re.sub(r"[\r\n]+", " ", str(value))
                f.write(f"{key}={one_line}\n")

def parse_field(body, heading):
    """Cell ids typed under the form's heading, upper-cased, in order, without repeats."""
    m = re.search(rf"### {re.escape(heading)}\s*\n(.*?)(?=\n###|\Z)", body or "", re.S)
    text = m.group(1) if m else ""
    if text.strip().lower() == "_no response_":
        return []
    seen, cells = set(), []
    for token in re.split(r"[\s,;]+", text.strip()):
        token = token.strip().upper()
        if token and token not in seen:
            seen.add(token)
            cells.append(token)
    return cells

def main():
    grid = load_grid()
    body = os.environ.get("ISSUE_BODY", "")
    split, searched, skip = set(grid.get("split", [])), set(grid.get("searched", [])), set(grid.get("skip", []))
    added, merged, skipped = [], [], []
    for token in parse_field(body, "Cells to Split"):
        base, _, error = parse_cell(grid, token)
        if error:
            skipped.append(f"- {error}")
        elif base in skip:
            skipped.append(f"- {token}: {base} is marked as water and has no restaurants to find")
        elif base in split or base in added:
            skipped.append(f"- {token}: {base} is already split")
        else:
            added.append(base)
    for token in parse_field(body, "Cells to Merge Back"):
        base, _, error = parse_cell(grid, token)
        if error:
            skipped.append(f"- {error}")
        elif base not in split or base in merged:
            skipped.append(f"- {token}: {base} is not split")
        else:
            merged.append(base)
    if added or merged:
        split = (split | set(added)) - set(merged)
        # a merged cell's children no longer exist, so their searched marks go with them
        searched = {c for c in searched if c.split("-")[0] not in merged or "-" not in c}
        grid["split"] = sorted(split, key=sort_key)
        grid["searched"] = sorted(searched, key=sort_key)
        save_grid(grid)
    set_output("count", len(added) + len(merged))
    n = grid["splitInto"]
    lines = []
    if added:
        lines.append(f"Split {', '.join(added)} into {n * n} cells each ({grid['cellKm'] / n:g} km).")
    if merged:
        lines.append(f"Merged {', '.join(merged)} back into one cell each ({grid['cellKm']:g} km).")
    if not lines:
        lines.append("No cells were changed.")
    if skipped:
        lines.append("\n## Skipped\n\n" + "\n".join(skipped))
    report = "\n".join(lines)
    print(report)
    set_output("report", report, multiline=True)

if __name__ == "__main__":
    main()
