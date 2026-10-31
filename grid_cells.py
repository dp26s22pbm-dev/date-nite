"""The grid behind datenite.us/grid/: cell ids, rectangles, and the grid.json file. Shared by split_cells.py and add_cell.py.

Columns are letters from the west edge (A, B, ... Z, AA), rows are numbers from the north edge, so G4 is the seventh
column, fourth row. A cell listed in grid.json "split" is divided into splitInto x splitInto children named a, b, c ...
in rows from its north-west corner (G4-a is the north-west child). The page computes the same ids in grid/index.html;
keep the two in step."""
import re
import json

GRID_FILE = "grid/grid.json"
CELL_RE = re.compile(r"([A-Z]+)([0-9]+)(?:-([A-Z]))?")

def load_grid(path=GRID_FILE):
    with open(path) as f:
        return json.load(f)

def save_grid(grid, path=GRID_FILE):
    with open(path, "w") as f:
        json.dump(grid, f, indent=2)
        f.write("\n")

def col_index(letters):
    """A -> 0, B -> 1, ... Z -> 25, AA -> 26."""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1

def col_name(i):
    s, i = "", i + 1
    while i > 0:
        s = chr(65 + (i - 1) % 26) + s
        i = (i - 1) // 26
    return s

def sort_key(cell_id):
    m = CELL_RE.fullmatch(cell_id.upper())
    return (len(m.group(1)), m.group(1), int(m.group(2)), m.group(3) or "")

def parse_cell(grid, token):
    """(base, child, error) for a typed cell id. `base` is the 4 km cell ("G4"), `child` the letter of a 1 km cell
    ("b" for G4-b) or None. error is a message when the id is not a cell of this grid, else None."""
    m = CELL_RE.fullmatch(token.strip().upper())
    if not m:
        return None, None, f"{token}: not a cell id (expected a letter and a number, such as G4, or G4-b for a smaller cell)"
    letters, row, child = m.group(1), int(m.group(2)) - 1, m.group(3)
    col = col_index(letters)
    if not (0 <= col < grid["cols"] and 0 <= row < grid["rows"]):
        return None, None, f"{token}: outside the grid ({grid['cols']} columns by {grid['rows']} rows)"
    n = grid["splitInto"]
    if child and ord(child) - 65 >= n * n:
        return None, None, f"{token}: a split cell only has children a to {chr(96 + n * n)}"
    return f"{letters}{row + 1}", child.lower() if child else None, None

def cell_id(base, child=None):
    return f"{base}-{child}" if child else base

def cell_bounds(grid, base, child=None):
    """(south, west, north, east) of the base cell, or of one of its children."""
    m = CELL_RE.fullmatch(base)
    col, row = col_index(m.group(1)), int(m.group(2)) - 1
    d_lat, d_lon = grid["dLat"], grid["dLon"]
    north = grid["origin"]["lat"] - row * d_lat
    west = grid["origin"]["lon"] + col * d_lon
    if child:
        n = grid["splitInto"]
        k = ord(child) - 97
        d_lat, d_lon = d_lat / n, d_lon / n
        north -= (k // n) * d_lat
        west += (k % n) * d_lon
    return north - d_lat, west, north, west + d_lon

def cell_of(grid, coords, splits=None):
    """The id of the finest cell holding [lon, lat], or None when the point is outside the grid."""
    splits = set(grid.get("split", [])) if splits is None else splits
    lon, lat = coords
    fc, fr = (lon - grid["origin"]["lon"]) / grid["dLon"], (grid["origin"]["lat"] - lat) / grid["dLat"]
    c, r = int(fc // 1), int(fr // 1)
    if not (0 <= c < grid["cols"] and 0 <= r < grid["rows"]):
        return None
    base = f"{col_name(c)}{r + 1}"
    if base not in splits:
        return base
    n = grid["splitInto"]
    sc, sr = min(n - 1, int((fc - c) * n)), min(n - 1, int((fr - r) * n))
    return cell_id(base, chr(97 + sr * n + sc))
