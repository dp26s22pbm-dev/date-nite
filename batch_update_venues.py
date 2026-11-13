import os
import sys
import json
import statistics
from datetime import date
from google import genai
from google.genai import types

import menu_log

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

try:
    from urls import TARGET_URLS as VENUE_URLS
except Exception as e:
    print(f"Failed to import TARGET_URLS from urls.py: {e}")
    VENUE_URLS = {}

DEFAULT_VENUE_LIMIT = 60
# priceSource values written by this refresh. A venue carrying one of them has search-derived (or re-read PDF) prices,
# so the next refresh may replace them. Any other source, or none, goes through protection_reason() first.
REFRESH_SOURCES = ("search", "pdf")
# Closure checks that cover venues the price refresh leaves alone (protected prices). They rotate by closureCheckedAt,
# which every closure check stamps, so every venue is checked in turn whether or not its prices are refreshed.
DEFAULT_CLOSURE_LIMIT = 40
CLOSURE_FLAGS_FILE = "closure_flags.json"

CLOSURE_PROMPT = """
Is the restaurant "{venue_name}" ({category}), located near latitude {lat}, longitude {lon} in {place}, permanently closed?
Use Google Search. Answer CLOSED only if a source says this location has permanently closed, has been replaced by a different business, or is "permanently closed" on Google Maps or Yelp. Temporary closures, renovations and seasonal hours are not closures. Answer OPEN if sources show it operating, and UNKNOWN if you cannot tell.
Return ONLY a valid JSON object: {{"status": "OPEN" | "CLOSED" | "UNKNOWN", "evidence": "one sentence naming the source"}}
Do not include markdown code block formatting.
"""

SEARCH_PRICING_PROMPT = """
Search for current menu pricing for "{venue_name}" in {city}.
Find real-world pricing for these categories:
1. apps: typical appetizer / starter item price (in USD, number only)
2. casualMains: standard, lower-cost main entree or sandwich prices (in USD, number only)
3. premiumMains: prices of the steak and expensive seafood entrees (for example filet, ribeye, strip, lobster, king crab, scallops, salmon, sea bass), in USD, number only. List the standard single-entree plate for each. Do not include chicken, pork or vegetarian dishes, combination plates or multi-protein platters. Exclude large-format or shared cuts (tomahawk, porterhouse for two), wagyu, market-price or "MP" items, surf-and-turf add-ons, seafood towers and platters for sharing. If the menu has no steak or expensive seafood, list its highest priced standard entrees instead.
4. drinks: standard cocktail or craft beverage prices (in USD, number only)
5. desserts: typical dessert prices (in USD, number only)

Return ONLY a valid JSON object matching this schema:
{{
  "apps": [float, ...],
  "casualMains": [float, ...],
  "premiumMains": [float, ...],
  "drinks": [float, ...],
  "desserts": [float, ...]
}}
Do not include markdown code block formatting or explanations. Output pure JSON.
"""

PDF_PROMPT = """
You are a restaurant menu pricing extraction engine. Analyze the provided menu content (text or PDF document) and extract typical menu prices into this JSON structure:
{
  "apps": [float, ...],
  "casualMains": [float, ...],
  "premiumMains": [float, ...],
  "drinks": [float, ...],
  "desserts": [float, ...]
}
Rules:
- "apps": Starters, sides, appetizers, soups, salads.
- "casualMains": Standard pastas, pizzas, handhelds, burgers, sandwiches.
- "premiumMains": Standard steaks (ribeye, strip, filet), prime chops, prime seafood (salmon, lobster), with the standard single-entree plate for each. Exclude combination or multi-protein plates, chicken and vegetarian dishes, large-format or shared cuts, wagyu, market-price items and sharing platters. If there are no premium luxury cuts, use the top 25% highest priced entrees.
- "drinks": Wine, beer, cocktails, sodas.
- "desserts": Tiramisu, cannoli, cakes, gelato.
Return strictly valid JSON with no markdown formatting.
"""

def calculate_tier_color(total_cost):
    if total_cost < 50:
        return "#38bdf8"
    elif total_cost < 100:
        return "#22c55e"
    elif total_cost < 150:
        return "#eab308"
    elif total_cost < 200:
        return "#f97316"
    else:
        return "#ef4444"

def venue_place(v):
    """"Elmhurst, IL": the venue's own town, so the search looks for the right restaurant."""
    town, state = (v.get("city") or "").strip(), (v.get("state") or "").strip()
    return ", ".join(p for p in (town, state) if p) or "the Chicago, IL area"


def log_index(entries):
    """({name key: latest menu-log record}, {name key: last logged final prices}). Matched by name: the venue_id in
    the log is not unique (grid runs that ran side by side handed out the same id)."""
    latest, finals = {}, {}
    for e in sorted(entries, key=lambda e: e.get("t", "")):
        key = menu_log.name_key(e.get("venue"))
        latest[key] = e
        if e.get("final"):
            finals[key] = e["final"]
    return latest, finals


def protection_reason(v, index):
    """Why this venue's saved prices must not be replaced by a web-search estimate, or None when a refresh may.

    The refresh stamps priceSource "search" (or "pdf") on what it writes, so those venues are fair game again. Edit Venue
    stamps "hand" or "menu". Anything else is judged from the menu log: prices that still equal what the importer read
    from the restaurant's own menu, or that differ from the importer's numbers (someone changed them), are protected.
    A venue with no log record has no known source and is refreshed as before."""
    source = v.get("priceSource")
    if source in REFRESH_SOURCES:
        return None
    if source == "hand":
        return "prices were typed in by hand"
    if source == "menu":
        return "prices were read from a menu someone pasted"
    latest, finals = index
    key = menu_log.name_key(v.get("name"))
    record, final = latest.get(key), finals.get(key)
    if record is None or not final:
        return None
    if not menu_log.still_estimates(v, final):
        return "saved prices differ from the importer's, so someone changed them"
    if record.get("outcome") in ("menu_read", "menu_read_partial"):
        return "prices were read from the restaurant's own menu"
    return None


def fetch_pricing_via_gemini_search(venue_name, city="Naperville, IL"):
    """Query Gemini with Google Search Grounding to find live menu pricing."""
    try:
        prompt = SEARCH_PRICING_PROMPT.format(venue_name=venue_name, city=city)
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1
            ),
        )
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`").removeprefix("json").strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"Gemini Search Grounding error for {venue_name}: {e}")
        return None

def closure_status(v):
    """Ask Gemini (with search) whether a venue has permanently closed. Returns ("closed", evidence), ("open", None)
    or ("error", None) when no answer could be read. Never edits the venue: a person reviews the flag and removes it."""
    try:
        lon, lat = v["coords"]
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=CLOSURE_PROMPT.format(venue_name=v["name"], category=v.get("category", "restaurant"), lat=lat, lon=lon, place=venue_place(v)),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0
            ),
        )
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`").removeprefix("json").strip()
        result = json.loads(raw_text)
        if str(result.get("status", "")).upper() == "CLOSED":
            return "closed", str(result.get("evidence", "")).strip() or "No source given."
        return "open", None
    except Exception as e:
        print(f"Closure check error for {v.get('name')}: {e}")
        return "error", None

def check_for_closure(v):
    """The evidence string when Gemini reports the venue CLOSED, else None."""
    status, evidence = closure_status(v)
    return evidence if status == "closed" else None

def run_closure_check(v, flags):
    """Check one venue, add a flag when it looks closed, and stamp closureCheckedAt unless the check could not be read
    (an unreadable answer must not push the venue to the back of the rotation). Returns True when the venue was stamped."""
    status, evidence = closure_status(v)
    if status == "closed":
        print(f"  Possible permanent closure: {evidence}")
        flags.append({"id": v.get("id"), "name": v.get("name", ""), "evidence": evidence})
    if status == "error":
        return False
    v["closureCheckedAt"] = date.today().isoformat()
    return True

def parse_pricing_from_pdf(pdf_bytes):
    """Parse pricing from a PDF using Gemini (no search grounding needed)."""
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[
                PDF_PROMPT,
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
            ]
        )
        clean = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean)
    except Exception as e:
        print(f"Gemini PDF parsing error: {e}")
        return None

def apply_pricing_to_venue(v, pricing, source="search"):
    """Update venue dict with median values from pricing data. Returns True if updated."""
    if not pricing or not pricing.get("casualMains"):
        return False

    casual_list = pricing.get("casualMains") or [19.00]
    v["casualMain"] = round(statistics.median(casual_list), 2)

    prem_list = pricing.get("premiumMains") or [round(v["casualMain"] * 1.6, 2)]
    v["premiumMain"] = round(statistics.median(prem_list), 2)

    apps_list = pricing.get("apps") or [round(v["casualMain"] * 0.55, 2)]
    v["app"] = round(statistics.median(apps_list), 2)

    drinks_list = pricing.get("drinks") or [8.00]
    v["drink"] = round(statistics.median(drinks_list), 2)

    desserts_list = pricing.get("desserts") or [8.00]
    v["dessert"] = round(statistics.median(desserts_list), 2)

    total_cost = round((v["app"] + (1 * v["casualMain"]) + (1 * v["premiumMain"]) + (2 * v["drink"]) + v["dessert"]) * 1.30)
    v["tierColor"] = calculate_tier_color(total_cost)
    v["lastUpdated"] = date.today().isoformat()
    v["priceSource"] = source
    return True

def select_venues(venues, target_venue, limit, skip_ids=()):
    """Pick which venues to refresh this run.

    A target name matches by substring and ignores the limit. Otherwise the
    `limit` least recently updated venues are chosen (never-updated first),
    so repeated runs rotate through the whole list. A limit of 0 means all.
    Venues whose ids are in `skip_ids` (protected prices) are left out before the limit applies, so they do not use
    up the run's slots. A target name ignores both.
    """
    if target_venue:
        return [v for v in venues if target_venue in v.get("name", "").lower()]
    ordered = sorted((v for v in venues if v.get("id") not in skip_ids), key=lambda v: v.get("lastUpdated", ""))
    return ordered if limit <= 0 else ordered[:limit]

def select_for_closure(venues, protected_ids, limit):
    """Protected venues (the price refresh skips them), least recently closure-checked first, never-checked first.
    A limit of 0 means all of them."""
    pool = sorted((v for v in venues if v.get("id") in protected_ids), key=lambda v: v.get("closureCheckedAt", ""))
    return pool if limit <= 0 else pool[:limit]

def main():
    target_venue = os.environ.get("TARGET_VENUE", "").strip().lower()

    with open("venues.json", "r") as f:
        venues = json.load(f)

    try:
        limit = int(os.environ.get("VENUE_LIMIT", "").strip() or DEFAULT_VENUE_LIMIT)
    except ValueError:
        limit = DEFAULT_VENUE_LIMIT

    protected = {}
    if not target_venue:
        entries = menu_log.load_entries()
        if not entries:
            # Without the log a venue read from a real menu looks like any other, so stop instead of overwriting it
            sys.exit("Could not read the menu-log branch, so protected prices cannot be told apart. Nothing was refreshed.")
        index = log_index(entries)
        protected = {v.get("id"): r for v in venues if (r := protection_reason(v, index))}
        print(f"Leaving {len(protected)} venue(s) alone because their prices were read from a real menu or changed by hand.")

    try:
        closure_limit = int(os.environ.get("CLOSURE_LIMIT", "").strip() or DEFAULT_CLOSURE_LIMIT)
    except ValueError:
        closure_limit = DEFAULT_CLOSURE_LIMIT

    selected = select_venues(venues, target_venue, limit, skip_ids=set(protected))
    closure_only = select_for_closure(venues, set(protected), closure_limit)
    print(f"Refreshing {len(selected)} of {len(venues)} venue(s); closure-checking {len(closure_only)} protected venue(s).")

    updated_count = 0
    checked_count = 0
    closure_flags = []
    for v in selected:
        venue_id = v.get("id")
        name = v.get("name", "")

        print(f"Refreshing pricing for: {name} (ID: {venue_id})")

        if run_closure_check(v, closure_flags):
            checked_count += 1

        # Step 1: Try direct PDF download if the registered URL is a PDF
        pdf_pricing = None
        url = VENUE_URLS.get(venue_id)
        if url and url.lower().endswith(".pdf"):
            import urllib.request
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as res:
                    pdf_pricing = parse_pricing_from_pdf(res.read())
            except Exception as e:
                print(f"  PDF fetch failed: {e}")

        if pdf_pricing and apply_pricing_to_venue(v, pdf_pricing, source="pdf"):
            print(f"  Updated {name} from PDF: Casual ${v['casualMain']}, Prime ${v['premiumMain']}")
            updated_count += 1
            continue

        # Step 2: Use Gemini Google Search Grounding as the primary method
        pricing = fetch_pricing_via_gemini_search(name, venue_place(v))

        if pricing and apply_pricing_to_venue(v, pricing):
            print(f"  Updated {name} via Gemini Search: Casual ${v['casualMain']}, Prime ${v['premiumMain']}, Drinks ${v['drink']}, App ${v['app']}, Color {v['tierColor']}")
            updated_count += 1
        else:
            print(f"  Could not retrieve new prices for {name}; keeping existing data.")

    for v in closure_only:
        print(f"Closure check only (prices left alone): {v.get('name', '')} (ID: {v.get('id')})")
        if run_closure_check(v, closure_flags):
            checked_count += 1

    with open(CLOSURE_FLAGS_FILE, "w") as f:
        json.dump(closure_flags, f, indent=2)

    if updated_count > 0 or checked_count > 0:
        with open("venues.json", "w") as f:
            json.dump(venues, f, indent=2)
        print(f"Saved venues.json: {updated_count} repriced, {checked_count} closure-checked, {len(closure_flags)} flagged.")

if __name__ == "__main__":
    main()
