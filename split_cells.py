"""Add cells to the "split" list in grid/grid.json. Read from the Add Split form (ISSUE_BODY, the lines under its
"Cells to Split" heading). Only base cells (A1, G4, ...) split; a child id such as G4-b is read as its parent G4.
Ids outside the grid or already split are reported and skipped. Writes nothing when nothing is left to add.

Outputs for the workflow (GITHUB_OUTPUT): count (cells added) and report (text for the pull request or issue comment)."""
import os
import re
import json

GRID_FILE = "grid/grid.json"

def set_output(key, value, multiline=False):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            if multiline:
                f.write(f"{key}<<EOF_{key}\n{value}\nEOF_{key}\n")
            else:
                one_line = re.sub(r"[\r\n]+", " ", str(value))
                f.write(f"{key}={one_line}\n")

def col_index(letters):
    """A -> 0, B -> 1, ... Z -> 25, AA -> 26. Matches colName() in grid/index.html."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1

def parse_cells(body):
    """Cell ids typed under the form's "Cells to Split" heading, upper-cased, in order, without repeats."""
    m = re.search(r"### Cells to Split\s*\n(.*?)(?=\n###|\Z)", body or "", re.S)
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
    with open(GRID_FILE) as f:
        grid = json.load(f)
    already = set(grid.get("split", []))
    added, skipped = [], []
    for token in parse_cells(os.environ.get("ISSUE_BODY", "")):
        base = token.split("-")[0]  # G4-B is read as G4
        m = re.fullmatch(r"([A-Z]+)([0-9]+)", base)
        if not m:
            skipped.append(f"- {token}: not a cell id (expected a letter and a number, such as G4)")
            continue
        col, row = col_index(m.group(1)), int(m.group(2)) - 1
        if not (0 <= col < grid["cols"] and 0 <= row < grid["rows"]):
            skipped.append(f"- {token}: outside the grid ({grid['cols']} columns by {grid['rows']} rows)")
        elif base in already or base in added:
            skipped.append(f"- {token}: {base} is already split")
        else:
            added.append(base)
    if added:
        grid["split"] = sorted(already | set(added), key=lambda c: (len(re.match(r"[A-Z]+", c).group()), c))
        with open(GRID_FILE, "w") as f:
            json.dump(grid, f, indent=2)
            f.write("\n")
    set_output("count", len(added))
    lines = []
    if added:
        n = grid["splitInto"]
        lines.append(f"Split {', '.join(added)} into {n * n} cells each ({grid['cellKm'] / n:g} km).")
    else:
        lines.append("No cells were split.")
    if skipped:
        lines.append("\n## Skipped\n\n" + "\n".join(skipped))
    report = "\n".join(lines)
    print(report)
    set_output("report", report, multiline=True)

if __name__ == "__main__":
    main()
