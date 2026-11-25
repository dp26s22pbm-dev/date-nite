"""Choose the grid cells the daily search (daily-search.yml) looks at next.

A cell is a candidate when it is not water, not already searched, not split (its smaller cells are candidates instead),
not holding as many venues as a cell takes (grid.json "fullAt"), and not part of an open Search Grid Cells pull request.
Candidates are ranked by how many venues sit in the cell and its eight neighbours, so the search works outward from
parts of the map that already have restaurants before it tries the edges; ties go in grid order.

The run does nothing when MAX_OPEN_CELL_PRS or more cell pull requests are open (default 3). Each one holds candidate
venues nobody has decided on yet, and they all edit grid/grid.json, so more of them only pile up.

Environment: MAX_CELLS (how many to pick; default 5), MAX_OPEN_CELL_PRS, GH_TOKEN (to list the open pull requests).
Outputs (to $GITHUB_OUTPUT and stdout): proceed, reason, cells (comma separated), body (the issue body, in the form
add_cell.py reads)."""
import os
import json

import add_city as ac
import add_cell
from grid_cells import load_grid, parse_cell, cell_id, cell_of, col_index, col_name, sort_key
from split_cells import set_output

NEIGHBOURS = [(dc, dr) for dc in (-1, 0, 1) for dr in (-1, 0, 1)]


def base_of(cid):
    return cid.split("-")[0]


def base_position(base):
    """(column index, row index) of a base cell id such as G4."""
    letters = "".join(ch for ch in base if ch.isalpha())
    return col_index(letters), int("".join(ch for ch in base if ch.isdigit())) - 1


def candidates(grid, venues, open_prs=()):
    """[(cell id, score)] best first. `venues` need a "coords" [lon, lat]."""
    splits, skip = set(grid.get("split", [])), set(grid.get("skip", []))
    searched, full_at = set(grid.get("searched", [])), grid["fullAt"]
    held, per_base = {}, {}
    for v in venues:
        cid = cell_of(grid, v["coords"], splits)
        if cid:
            held[cid] = held.get(cid, 0) + 1
            per_base[base_of(cid)] = per_base.get(base_of(cid), 0) + 1
    n = grid["splitInto"]
    out = []
    for col in range(grid["cols"]):
        for row in range(grid["rows"]):
            base = f"{col_name(col)}{row + 1}"
            if base in skip:
                continue
            ids = [cell_id(base, chr(97 + k)) for k in range(n * n)] if base in splits else [base]
            near = sum(per_base.get(f"{col_name(col + dc)}{row + dr + 1}", 0) for dc, dr in NEIGHBOURS)
            for cid in ids:
                if cid in searched or cid in open_prs or held.get(cid, 0) >= full_at:
                    continue
                out.append((cid, near))
    out.sort(key=lambda item: (-item[1], sort_key(item[0])))
    return out


def pick(grid, venues, limit, open_prs=()):
    return [cid for cid, _ in candidates(grid, venues, open_prs)[:limit]]


def issue_body(cells):
    """The text of a Search Grid Cells issue, as add_cell.py reads it."""
    return f"### Cells to Search\n\n{', '.join(cells)}\n"


def main():
    grid = load_grid()
    limit = int(os.environ.get("MAX_CELLS", "5") or 5)
    max_open = int(os.environ.get("MAX_OPEN_CELL_PRS", "3") or 3)
    open_prs = add_cell.open_cell_prs()
    open_count = len(set(open_prs.values()))
    cells, reason = [], ""
    if limit < 1:
        reason = "the budget allows no cells"
    elif open_count >= max_open:
        reason = f"{open_count} cell pull requests are still open; review them first"
    else:
        venues = json.load(open("venues.json")) + ac.load_pending()
        cells = pick(grid, venues, limit, open_prs)
        if not cells:
            reason = "no unsearched cells are left"
    outputs = {"proceed": "true" if cells else "false", "reason": reason, "cells": ",".join(cells)}
    for k, v in outputs.items():
        print(f"{k}={v}")
        set_output(k, v)
    if cells:
        set_output("body", issue_body(cells), multiline=True)


if __name__ == "__main__":
    main()
