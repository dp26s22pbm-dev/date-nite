"""Open one venue's Google Maps listing the way the importer does and show what its Menu tab gave.

  python maps_menu_probe.py "Tequila Azteca Restaurant" "Willowbrook, IL" [--place-id ID] [--maps-url URL]

Needs no API keys: it stops before Gemini sorts the prices into categories, and prints the text the browser read, the
number of price-like figures in it (the importer needs at least 6) and the reason when nothing was read. Run it from
the "Maps Menu Probe" workflow, since Google treats a GitHub runner differently from a laptop.
"""
import argparse
import os
import sys
import types

# add_venue_from_issue creates a Gemini client at import time; this script has no key and does not need one
for _mod in ("google", "google.genai", "google.genai.types"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
_genai = sys.modules["google.genai"]
_genai.Client = lambda **kw: None
_genai.types = sys.modules["google.genai.types"]
sys.modules["google"].genai = _genai

os.environ.setdefault("MAPS_MENU_DEBUG", "1")
import add_venue_from_issue as importer  # noqa: E402
import menu_log  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("where", help="town and state, or the street address")
    ap.add_argument("--place-id", default="")
    ap.add_argument("--maps-url", default="")
    ap.add_argument("--show", type=int, default=6000, help="how many characters of the text to print")
    args = ap.parse_args()

    url = importer.maps_listing_url(args.name, args.where, args.maps_url, args.place_id)
    print(f"Listing address: {url}")
    if not url:
        sys.exit("Nothing to open: give a town, a Maps link or a place id.")
    # Collect the log steps in memory so the reason for a miss can be printed; nothing is written or published
    menu_log._cur = {"steps": []}
    text = importer.render_maps_menu_text(url, args.name)
    steps = menu_log._cur["steps"]
    menu_log._cur = None
    print("Log steps: " + ("; ".join(f"{s['step']}: {s['result']}" for s in steps) or "none"))
    if not text:
        print("RESULT: no menu text was read.")
        return
    n = importer.price_count(text)
    print(f"RESULT: {len(text)} characters, {n} price-like figures" + ("" if n >= 6 else " (too few; the importer needs 6)"))
    print("----- text read -----")
    print(text[:args.show])


if __name__ == "__main__":
    main()
