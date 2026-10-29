"""Propose the first venues for a new city: find its best restaurants on Google Places, rank them by rating weighted
by review count (at most MAX_PER_CUISINE of one cuisine), then add each one with add_venue_from_issue.py.

Ratings and review counts only decide the ranking; they are never written to venues.json. The workflow
(add-city.yml) commits the result to a branch and opens a pull request for review."""
import os
import re
import sys
import json
import subprocess
import tempfile
import urllib.request
import urllib.error

import places_usage

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.displayName", "places.formattedAddress", "places.location", "places.websiteUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "places.primaryType", "places.types", "places.regularOpeningHours",
    "nextPageToken",
])

COUNT = int(os.environ.get("VENUE_COUNT", "5"))
MAX_PER_CUISINE = int(os.environ.get("MAX_PER_CUISINE", "2"))
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

def name_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())

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

def search_city(city, api_key):
    """Restaurants Places lists for the city, up to MAX_PAGES pages."""
    places, token = [], None
    for _ in range(MAX_PAGES):
        payload = {"textQuery": f"restaurants in {city}", "includedType": "restaurant", "pageSize": 20}
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

def eligible(place, city_name, saved_names):
    loc = place.get("location") or {}
    name = (place.get("displayName") or {}).get("text", "")
    if place.get("businessStatus") != "OPERATIONAL" or not name or "latitude" not in loc:
        return False
    if place.get("rating") is None or (place.get("userRatingCount") or 0) < MIN_REVIEWS:
        return False
    if EXCLUDED_TYPES & set(place.get("types") or []) or place.get("primaryType") in EXCLUDED_TYPES:
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
    return name_key(name) not in saved_names

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

def pick(ranked, count=COUNT, per_cuisine=MAX_PER_CUISINE):
    """The first `count` of the ranked places, skipping any whose cuisine already has `per_cuisine` picks."""
    chosen, used = [], {}
    for place, score in ranked:
        cuisine = cuisine_key(place)
        if cuisine and used.get(cuisine, 0) >= per_cuisine:
            continue
        chosen.append((place, score))
        if cuisine:
            used[cuisine] = used.get(cuisine, 0) + 1
        if len(chosen) == count:
            break
    return chosen

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

def add_venue(place, work_dir, number):
    """Run the importer for one place. Returns its outputs dict (venue_name, summary, needs_menu, ...), or {"error": ...} on failure."""
    out_path = os.path.join(work_dir, f"output-{number}.txt")
    open(out_path, "w").close()
    # The importer reuses the prices in this file when it finds one, so every venue needs a file of its own
    env = {**os.environ, "ISSUE_BODY": issue_body(place), "ISSUE_TITLE": "", "GITHUB_OUTPUT": out_path,
           "PRICING_CACHE_FILE": os.path.join(work_dir, f"pricing-{number}.json")}
    here = os.path.dirname(os.path.abspath(__file__))
    result = subprocess.run([sys.executable, os.path.join(here, "add_venue_from_issue.py")], env=env)
    outputs = read_outputs(out_path)
    return outputs if result.returncode == 0 else {"error": outputs.get("error", "see the workflow log")}

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

    with open("venues.json") as f:
        saved_names = {name_key(v["name"]) for v in json.load(f)}

    places = search_city(city, api_key)
    candidates = [p for p in places if eligible(p, city_name, saved_names)]
    chosen = pick(rank(candidates))
    print(f"{len(places)} places found, {len(candidates)} eligible, {len(chosen)} chosen")
    set_output("count", len(chosen))
    if not chosen:
        set_output("report", f"No restaurants in {city} qualified (open, with at least {MIN_REVIEWS} reviews, and not already on the map). "
                             "Check the spelling of the city and state.", multiline=True)
        return

    sections, failed = [], 0
    with tempfile.TemporaryDirectory() as work_dir:
        for rank_no, (place, score) in enumerate(chosen, 1):
            name = place["displayName"]["text"]
            print(f"\n=== {rank_no}. {name} (score {score:.2f}) ===")
            outputs = add_venue(place, work_dir, rank_no)
            if outputs.get("error") and not outputs.get("summary"):
                failed += 1
                sections.append(f"## {rank_no}. {name}\n\nCould not be added: {outputs['error']}")
                continue
            sections.append(f"## {rank_no}. {name}\n\nRanking score {score:.2f}\n\n{outputs.get('summary', '')}")

    intro = (f"Top {len(chosen)} restaurants for {city}, ranked by Google rating weighted by review count "
             f"(at least {MIN_REVIEWS} reviews, at most {MAX_PER_CUISINE} of one cuisine). Ratings were used only for the ranking and are not saved.\n\n"
             "Review the prices and pins below before merging. Venues you don't want can be removed from `venues.json` in this branch.")
    set_output("report", intro + "\n\n" + "\n\n".join(sections), multiline=True)
    set_output("added", len(chosen) - failed)

if __name__ == "__main__":
    main()
