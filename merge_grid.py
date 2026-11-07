"""Three-way merge for grid/grid.json, used by .github/workflows/refresh-cell-prs.yml.

Search Grid Cells and Split Grid Cells pull requests each edit the sorted "searched" (or "split") list in
grid/grid.json. When another such pull request merges first, git sees two edits near each other in the same list and
reports a conflict. The lists are sets of cell ids, so they can always be merged: a cell either side added is kept, a
cell either side removed is dropped. Any other field takes the branch's value if only the branch changed it, otherwise
main's.

    python merge_grid.py BASE BRANCH MAIN OUT     # four file paths; OUT may equal BRANCH
"""
import json
import sys

from grid_cells import sort_key

LIST_FIELDS = ("searched", "split", "skip")

def merge_list(base, mine, theirs):
    base, mine, theirs = set(base), set(mine), set(theirs)
    merged = (base & mine & theirs) | (mine - base) | (theirs - base)
    return sorted(merged, key=sort_key)

def merge_grids(base, mine, theirs):
    """`mine` is the pull request branch, `theirs` is main. All three are parsed grid.json dicts."""
    out = {}
    for key in list(mine) + [k for k in theirs if k not in mine]:
        if key in LIST_FIELDS:
            out[key] = merge_list(base.get(key, []), mine.get(key, []), theirs.get(key, []))
        elif mine.get(key) == base.get(key):
            out[key] = theirs.get(key, mine.get(key))
        elif theirs.get(key) == base.get(key):
            out[key] = mine[key]
        else:
            out[key] = theirs.get(key, mine[key])  # both changed it: main wins
    return out

def main(argv):
    if len(argv) != 5:
        print(__doc__)
        return 2
    base_p, mine_p, theirs_p, out_p = argv[1:]
    loaded = []
    for p in (base_p, mine_p, theirs_p):
        with open(p) as f:
            loaded.append(json.load(f))
    merged = merge_grids(*loaded)
    with open(out_p, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
