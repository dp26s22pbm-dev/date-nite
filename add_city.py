"""Propose the first venues for a new city: find its best restaurants on Google Places, rank them by rating weighted
by review count (at most MAX_PER_CUISINE of one cuisine), then add each one with add_venue_from_issue.py.

Ratings and review counts only decide the ranking; they are never written to venues.json. The importer works on a
scratch copy of venues.json, and the new venues are saved without ids to pending-venues/<city>-<issue>.json. The
workflow (add-city.yml) commits that file to a branch and opens a pull request for review. After the PR merges,
fold_pending_venues.py numbers the venues and moves them into venues.json, so two city PRs never collide."""
import os
import re
import sys
import json
import subprocess
import shutil
import tempfile
import urllib.request
import urllib.error

import places_usage
import pricing_rules

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.displayName", "places.formattedAddress", "places.location", "places.websiteUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "places.primaryType", "places.types", "places.regularOpeningHours",
    "places.priceLevel", "places.dineIn", "places.reservable", "places.servesBeer", "places.servesWine",
    "nextPageToken",
])

COUNT = int(os.environ.get("VENUE_COUNT", "5"))
MAX_PER_CUISINE = int(os.environ.get("MAX_PER_CUISINE", "2"))
# National fast-food and fast-casual chains that never qualify. Higher-end fast casual (Shake Shack, Velvet Taco, local
# taquerias) is allowed; a priced venue is dropped only when two people spend less than MIN_TOTAL_FOR_TWO on the
# importer's standard order (appetizer, two mains, two drinks, dessert, plus tax and tip). A dropped venue is replaced by
# the next-best candidate, up to MAX_PRICED venues priced per city.
MIN_TOTAL_FOR_TWO = int(os.environ.get("MIN_TOTAL_FOR_TWO", "35"))
MAX_PRICED = int(os.environ.get("MAX_PRICED", "10"))
# Places gives signs of counter service (see counter_service()); such a place is listed under Skipped. Set to 0 to turn off.
SKIP_COUNTER_SERVICE = os.environ.get("SKIP_COUNTER_SERVICE", "1") != "0"
BLOCKED_CHAINS = re.compile(
    r"\b(mcdonalds|burger king|wendys|chick fil a|chipotle|taco bell|subway|kfc|popeyes|arbys|sonic drive|"
    r"panda express|qdoba|panera|jimmy johns|jersey mikes|dunkin|starbucks|wingstop|bibibop|sweetgreen|noodles (and|&) company|moes southwest|potbellys?|five guys|culvers|freddys|mod pizza|portillos|jets pizza|little caesars|domino s|dominos|pizza hut|papa johns|wendy s|rubio s|el pollo loco|del taco|taco john s|white castle|jack in the box|raising canes|zaxby s|firehouse subs|which wich|jasons deli|tropical smoothie|smoothie king|pei wei|buffalo wild wings)\b", re.I)
# A dinner place is open at DINNER_HOUR (24h clock) on at least DINNER_DAYS days of the week
DINNER_HOUR = int(os.environ.get("DINNER_HOUR", "19"))
DINNER_DAYS = int(os.environ.get("DINNER_DAYS", "3"))
BREAKFAST_TYPES = {"breakfast_restaurant", "brunch_restaurant"}
BREAKFAST_NAME = re.compile(r"\b(breakfast|brunch|pancakes?|waffles?|omelet|omelette|bagels?)\b", re.I)

# Places returns 20 results per page and at most 3 pages; each page is one billed request
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3"))
# A rating from a handful of reviews says little: the weighted score pulls such places toward the city average
MIN_REVIEWS = int(os.environ.get("MIN_REVIEWS", "100"))

# Place types that are not sit-down restaurants
EXCLUDED_TYPES = {"fast_food_restaurant", "coffee_shop", "cafe", "bakery", "ice_cream_shop", "meal_takeaway",
                  "convenience_store", "grocery_store", "gas_station"}
# Types too broad to say what the cuisine is; such a place never counts against the per-cuisine cap
# Primary types that are not named "..._restaurant" but are still places to eat
FOOD_PRIMARY_TYPES = {"steak_house", "bar_and_grill", "pub", "gastropub", "brewpub", "wine_bar", "diner", "buffet"}
GENERIC_TYPES = {"restaurant", "food", "establishment", "point_of_interest", "bar_and_grill", "bar", "meal_delivery"}

PENDING_DIR = "pending-venues"
# Venues someone decided to keep off the map: [{"name": ..., "city": optional, "reason": optional}]. Without a city,
# the name is skipped everywhere; with one, only in that city. A place is skipped when its name starts with an entry's name
# (case, spaces and punctuation ignored), so "Vasco's Peri Peri" also covers "Vasco's Peri Peri Lombard".
EXCLUDED_FILE = "excluded-venues.json"

def load_pending():
    """Venues waiting in pending-venues/ to be folded into venues.json, as a list."""
    venues = []
    if os.path.isdir(PENDING_DIR):
        for name in sorted(os.listdir(PENDING_DIR)):
            if name.endswith(".json"):
                with open(os.path.join(PENDING_DIR, name)) as f:
                    venues += json.load(f)
    return venues

def load_excluded(city_name):
    """name_key() prefixes of the excluded venues that apply to city_name."""
    if not os.path.exists(EXCLUDED_FILE):
        return set()
    with open(EXCLUDED_FILE) as f:
        entries = json.load(f)
    return {name_key(e["name"]) for e in entries
            if name_key(e["name"]) and (not e.get("city") or e["city"].strip().lower() == city_name.lower())}

def name_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())

def open_city_prs(slug, prefix="add-city"):
    """URLs of open pull requests this workflow already made for the city (branches <prefix>/<slug>-...). Their venues
    are not on main yet, so the saved-name check cannot see them and a repeat run would price the same places again.
    Returns [] when gh is unavailable or fails, so a lookup problem never blocks a run."""
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "url,headRefName"],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return []
        return [pr["url"] for pr in json.loads(out.stdout or "[]")
                if pr.get("headRefName", "").startswith(f"{prefix}/{slug}-")]
    except Exception:
        return []

def set_output(key, value, multiline=False):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            if multiline:
                f.write(f"{key}<<EOF_{key}\n{value}\nEOF_{key}\n")
            else:
                one_line = re.sub(r"[\r\n]+", " ", str(value))  # computed outside the f-string: Python 3.11 forbids backslashes inside one
                f.write(f"{key}={one_line}\n")

def read_outputs(path):
    """Parse a GITHUB_OUTPUT file written by add_venue_from_issue.py into a dict."""
    out, key, lines = {}, None, []
    with open(path) as f:
        for line in f.read().splitlines():
            if key is not None:
                if line == f"EOF_{key}":
                    out[key], key, lines = "\n".join(lines), None, []
                else:
                    lines.append(line)
            elif "<<EOF_" in line:
                key = line.split("<<EOF_", 1)[0]
            elif "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    return out

def search_city(city, api_key, rectangle=None):
    """Restaurants Places lists for the city, up to MAX_PAGES pages. With `rectangle` (south, west, north, east) the
    search is limited to that box instead (add_cell.py), and `city` is ignored."""
    places, token = [], None
    for _ in range(MAX_PAGES):
        payload = {"textQuery": f"restaurants in {city}", "includedType": "restaurant", "pageSize": 20}
        if rectangle:
            south, west, north, east = rectangle
            payload["textQuery"] = "restaurants"
            payload["locationRestriction"] = {"rectangle": {"low": {"latitude": south, "longitude": west},
                                                            "high": {"latitude": north, "longitude": east}}}
        if token:
            payload["pageToken"] = token
        req = urllib.request.Request(PLACES_URL, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELDS})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            places_usage.record(FIELDS, e.code)
            raise
        except Exception:
            places_usage.record(FIELDS, 0)
            raise
        places_usage.record(FIELDS, 200)
        places += data.get("places") or []
        token = data.get("nextPageToken")
        if not token:
            break
    return places

def cuisine_key(place):
    """The place's most specific cuisine type (e.g. italian_restaurant), or None when Google gives only generic ones."""
    primary = place.get("primaryType")
    if primary and primary not in GENERIC_TYPES:
        return primary
    return None

def serves_dinner(place):
    """True when the place is open at DINNER_HOUR on at least DINNER_DAYS days a week, False when it is not, and None
    when Places gives no opening hours (the caller then relies on the place types and name alone)."""
    periods = (place.get("regularOpeningHours") or {}).get("periods")
    if not periods:
        return None
    week = 7 * 1440
    spans = []
    for period in periods:
        opened = period.get("open") or {}
        if "day" not in opened:
            continue
        start = opened["day"] * 1440 + opened.get("hour", 0) * 60 + opened.get("minute", 0)
        closed = period.get("close")
        if not closed:  # open around the clock
            return True
        end = closed["day"] * 1440 + closed.get("hour", 0) * 60 + closed.get("minute", 0)
        spans.append((start, end if end > start else end + week))
    days = 0
    for day in range(7):
        moment = day * 1440 + DINNER_HOUR * 60
        # a period that runs past Saturday night wraps into the next week
        if any(s <= moment < e or s <= moment + week < e for s, e in spans):
            days += 1
    return days >= DINNER_DAYS

def eligible(place, city_name, saved_names, excluded=()):
    loc = place.get("location") or {}
    name = (place.get("displayName") or {}).get("text", "")
    if place.get("businessStatus") != "OPERATIONAL" or not name or "latitude" not in loc:
        return False
    if place.get("rating") is None or (place.get("userRatingCount") or 0) < MIN_REVIEWS:
        return False
    if EXCLUDED_TYPES & set(place.get("types") or []) or place.get("primaryType") in EXCLUDED_TYPES:
        return False
    if BLOCKED_CHAINS.search(re.sub(r"['\u2019]", "", name).replace("-", " ")):
        return False
    # Places lists a golf course or hotel with a restaurant among its types; keep it only if eating is its main business
    primary = place.get("primaryType")
    if primary and "restaurant" not in primary and primary not in FOOD_PRIMARY_TYPES:
        return False
    # Breakfast and brunch places: by type, by name, or because they close before dinner
    if BREAKFAST_TYPES & set(place.get("types") or []) or place.get("primaryType") in BREAKFAST_TYPES:
        return False
    if BREAKFAST_NAME.search(name) or serves_dinner(place) is False:
        return False
    # The search can spill into neighbouring towns; the formatted address names the city
    if city_name.lower() not in (place.get("formattedAddress") or "").lower():
        return False
    return name_key(name) not in saved_names and not name_key(name).startswith(tuple(excluded))

def rank(places):
    """Places sorted by the weighted score (v/(v+m))*R + (m/(v+m))*C, best first. R is the rating, v the review count,
    m MIN_REVIEWS and C the average rating of all candidates. Returns (place, score) pairs."""
    if not places:
        return []
    mean = sum(p["rating"] for p in places) / len(places)
    scored = []
    for p in places:
        v = p["userRatingCount"]
        scored.append((p, v / (v + MIN_REVIEWS) * p["rating"] + MIN_REVIEWS / (v + MIN_REVIEWS) * mean))
    return sorted(scored, key=lambda s: s[1], reverse=True)

def cap_reached(place, used, per_cuisine=MAX_PER_CUISINE):
    """True when `per_cuisine` venues of this place's cuisine are already kept. Places with only a generic type never hit the cap."""
    cuisine = cuisine_key(place)
    return bool(cuisine) and used.get(cuisine, 0) >= per_cuisine

def counter_service(place):
    """True when Places shows signs of counter service: no dine-in, or a cheap place ($ price level) that serves neither
    beer nor wine. Reservations are not considered: Google marks some counter-service places reservable (Yummi Wraps).
    A field Places leaves out counts as unknown, never as false, so a place with no price level is not caught by the
    second test."""
    if place.get("dineIn") is False:
        return True
    return (place.get("priceLevel") == "PRICE_LEVEL_INEXPENSIVE"
            and not (place.get("servesBeer") or place.get("servesWine")))

def total_for_two(venue):
    """The importer's standard order for two (appetizer, two mains, two drinks, dessert) with 30% tax and tip."""
    return round((venue["app"] + pricing_rules.casual_count(venue) * venue["casualMain"] + venue["premiumMain"] + 2 * venue["drink"] + venue["dessert"]) * 1.30)

def too_cheap(venue):
    """True when a priced venue is so cheap (see MIN_TOTAL_FOR_TWO) that it is counter-service rather than a date spot."""
    return total_for_two(venue) < MIN_TOTAL_FOR_TWO

def issue_body(place):
    """The form text add_venue_from_issue.py reads. Coordinates and website come from Places; the cuisine label is
    left blank so the importer picks one that fits the labels already on the map."""
    loc = place["location"]
    fields = [
        ("Restaurant Name", place["displayName"]["text"]),
        ("Street Address or City", place.get("formattedAddress", "")),
        ("Coordinates or Google Maps Link (Optional)", f"{loc['latitude']}, {loc['longitude']}"),
        ("Website URL", place.get("websiteUri", "")),
    ]
    return "\n\n".join(f"### {label}\n\n{value or '_No response_'}" for label, value in fields)

# The importer's report is written for a single-venue issue, where it can close the issue and take a reply with the menu
# text. In a city pull request neither happens, so these lines are reworded to match what the reviewer can do.
REPORT_REWORDS = (
    ("\n\n### 👉 Action needed: reply with the menu\n\n"
     "These prices are estimates. Open the venue's menu page, select all, copy, and paste it as a reply here. "
     "I will update the prices and close this issue.",
     "\n\n### 👉 Prices are estimates\n\nCheck them against the venue's menu and correct the entry before merging."),
    ("If they are wrong, reply with the menu text (open the venue's menu page, select all, copy, paste) "
     "and I will update them. If they are fine, close this issue.",
     "If they are wrong, correct the entry before merging."),
    ("Nothing else is needed. Closing this issue.", "Nothing else is needed."),
    ("(best guess; edit venues.json if it is off)", "(best guess; correct the entry if it is off)"),
    ("Send a share link of the place's page or exact coordinates to fix it", "Correct the coordinates in the entry if the pin is off"),
    ("or send a share link of the place's page to fix it", "or correct the coordinates in the entry"),
    ("Add one with the full form's Website URL field", "Add a website to the entry if you have one"),
)

def reword_for_city_pr(summary):
    """The importer's summary with its issue-only instructions replaced (see REPORT_REWORDS)."""
    for old, new in REPORT_REWORDS:
        summary = summary.replace(old, new)
    return summary

def add_venue(place, work_dir, number, venues_file):
    """Run the importer for one place. Returns its outputs dict (venue_name, summary, needs_menu, ...), or {"error": ...} on failure."""
    out_path = os.path.join(work_dir, f"output-{number}.txt")
    open(out_path, "w").close()
    # The importer reuses the prices in this file when it finds one, so every venue needs a file of its own
    env = {**os.environ, "ISSUE_BODY": issue_body(place), "ISSUE_TITLE": "", "GITHUB_OUTPUT": out_path,
           "PRICING_CACHE_FILE": os.path.join(work_dir, f"pricing-{number}.json"), "VENUES_FILE": venues_file}
    here = os.path.dirname(os.path.abspath(__file__))
    result = subprocess.run([sys.executable, os.path.join(here, "add_venue_from_issue.py")], env=env)
    outputs = read_outputs(out_path)
    return outputs if result.returncode == 0 else {"error": outputs.get("error", "see the workflow log")}

def price_and_keep(ranked, work_dir, venues_file, count=COUNT, max_priced=MAX_PRICED, number_start=0):
    """Run the importer on the ranked (place, score) pairs, best first, until `count` venues are kept or `max_priced`
    have been priced. Each kept venue is appended to venues_file (the scratch copy of venues.json); a venue dropped for
    price or counter service is taken back out. `number_start` keeps the importer's per-venue files apart when this is
    called more than once in one work_dir. Returns (sections, skipped, kept, priced): the report text for each kept
    venue, the report lines for the skipped ones, and the two counts."""
    sections, skipped, kept, used, priced = [], [], 0, {}, 0
    for place, score in ranked:
        if kept >= count or priced >= max_priced:
            break
        if cap_reached(place, used):
            continue
        name = place["displayName"]["text"]
        # Shown in the log so a place that slips through can be traced to what Google reported (None means Google left it out)
        print(f"\nPlaces data for {name}: primaryType={place.get('primaryType')} priceLevel={place.get('priceLevel')} "
              f"dineIn={place.get('dineIn')} reservable={place.get('reservable')} servesBeer={place.get('servesBeer')} "
              f"servesWine={place.get('servesWine')} types={place.get('types')}")
        # Checked before pricing, so a skipped place costs no importer or Gemini request
        if SKIP_COUNTER_SERVICE and counter_service(place):
            print(f"\nSkipped {name}: looks like counter service")
            skipped.append(f"- {name}: looks like counter service (cheap with no beer or wine, or no dine-in). "
                           "Add it with the Add Venue form if it is a sit-down place")
            continue
        priced += 1
        print(f"\n=== {name} (score {score:.2f}) ===")
        outputs = add_venue(place, work_dir, number_start + priced, venues_file)
        if outputs.get("error") and not outputs.get("summary"):
            skipped.append(f"- {name}: could not be added ({outputs['error']})")
            continue
        with open(venues_file) as f:
            venues = json.load(f)
        venue = venues[-1]
        # A venue with no menu or web-search prices only has the importer's typical defaults, which look like real
        # prices on the map. It is listed under Skipped instead; the Add Venue form can add it once someone has the menu.
        if outputs.get("prices_found") == "false":
            reason = "no prices could be found, so only typical defaults were possible. Add it with the Add Venue form and the menu text"
        elif pricing_rules.looks_like_counter_menu(venue):
            reason = (f"looks like counter service: its most expensive entrees are about ${venue['premiumMain']}. "
                      "Add it with the Add Venue form if it is a sit-down place")
        elif too_cheap(venue):
            reason = f"too cheap for a date spot (about ${total_for_two(venue)} for two)"
        else:
            reason = None
        if reason:
            with open(venues_file, "w") as f:
                json.dump(venues[:-1], f)
            print(f"Dropped {name}: {reason}")
            skipped.append(f"- {name}: {reason}")
            continue
        kept += 1
        if cuisine_key(place):
            used[cuisine_key(place)] = used.get(cuisine_key(place), 0) + 1
        sections.append(f"## {kept}. {name}\n\nRanking score {score:.2f}\n\n{reword_for_city_pr(outputs.get('summary', ''))}")
    return sections, skipped, kept, priced

def main():
    city = os.environ.get("CITY", "").strip()
    if not city:
        sys.exit("CITY is empty")
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        sys.exit("GOOGLE_PLACES_API_KEY is not set")
    city_name = city.split(",")[0].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    set_output("slug", slug)

    # Checked before any Places or Gemini request. Close or merge the open pull request to run the city again.
    existing = open_city_prs(slug)
    if existing:
        print(f"Open pull request already exists for {city}: {existing[0]}")
        set_output("count", 0)
        set_output("report", f"A pull request for {city} is already open: {existing[0]}. Merge or close it, then reopen "
                             "this issue to run the city again. Nothing was looked up or priced.", multiline=True)
        return

    with open("venues.json") as f:
        saved = json.load(f)
    saved += load_pending()  # venues from other city pull requests that have merged but are not folded in yet
    saved_names = {name_key(v["name"]) for v in saved}
    excluded = load_excluded(city_name)

    places = search_city(city, api_key)
    candidates = [p for p in places if eligible(p, city_name, saved_names, excluded)]
    ranked = rank(candidates)
    print(f"{len(places)} places found, {len(candidates)} eligible")
    if not ranked:
        set_output("count", 0)
        set_output("report", f"No restaurants in {city} qualified (open, with at least {MIN_REVIEWS} reviews, and not already on the map). "
                             "Check the spelling of the city and state.", multiline=True)
        return

    with tempfile.TemporaryDirectory() as work_dir:
        # The importer numbers and checks new venues against this scratch copy; venues.json itself stays untouched
        venues_file = os.path.join(work_dir, "venues.json")
        with open(venues_file, "w") as f:
            json.dump(saved, f)
        sections, skipped, kept, priced = price_and_keep(ranked, work_dir, venues_file)
        with open(venues_file) as f:
            added = json.load(f)[len(saved):]
    # Ids are handed out when the pull request merges (fold_pending_venues.py), so none are saved here
    for venue in added:
        venue.pop("id", None)
    pending_file = ""
    if added:
        os.makedirs(PENDING_DIR, exist_ok=True)
        pending_file = os.path.join(PENDING_DIR, f"{slug}-{os.environ.get('ISSUE_NUMBER', '0')}.json")
        with open(pending_file, "w") as f:
            json.dump(added, f, indent=2)
            f.write("\n")
    set_output("pending_file", pending_file)
    set_output("count", len(added))  # the workflow opens a pull request only when something was added

    intro = (f"Top {len(added)} restaurants for {city}, ranked by Google rating weighted by review count "
             f"(at least {MIN_REVIEWS} reviews, at most {MAX_PER_CUISINE} of one cuisine, no national fast-food chains, nothing under ${MIN_TOTAL_FOR_TWO} for two, no venue without any prices found, no counter-service places). "
             "Ratings were used only for the ranking and are not saved.\n\n"
             f"Review the prices and pins below before merging. The venues are in `{pending_file}`; delete an entry there to drop it. "
             "They get their ids and join `venues.json` automatically after the merge.")
    tail = ("\n\n## Skipped\n\n" + "\n".join(skipped)) if skipped else ""
    set_output("report", intro + "\n\n" + "\n\n".join(sections) + tail, multiline=True)

if __name__ == "__main__":
    main()
