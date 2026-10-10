import os
import re
import json
import statistics
import urllib.request
import urllib.parse
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

SEARCH_PRICING_PROMPT = """
Search for current menu pricing for "{venue_name}" in {city}.
Find real-world pricing for these categories:
1. apps: typical appetizer / starter item price (in USD, number only)
2. casualMains: standard, lower-cost main entree or sandwich prices (in USD, number only)
3. premiumMains: top-tier entree or steak/seafood/specialty main prices (in USD, number only)
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
- "casualMains": Standard pastas, pizzas, handhelds, burgers, chicken entrees, or basic sandwiches.
- "premiumMains": Steaks (ribeye, strip, filet), veal chops, prime seafood (salmon, lobster, sea bass), or specialty combo platters. If there are no premium luxury cuts, use the top 25% highest priced entrees.
- "drinks": House wine, draft beer, specialty cocktails, or soft drinks.
- "desserts": Tiramisu, cannoli, cakes, gelato.
Return strictly valid JSON with no markdown formatting.
"""

def extract_field(body, header):
    pattern = rf"### {re.escape(header)}\s*\n\s*(.*?)(?=\n###|\Z)"
    match = re.search(pattern, body, re.DOTALL)
    return match.group(1).strip() if match else ""

_expand_cache = {}

def is_maps_link(s):
    return bool(s) and ("maps" in s or "goo.gl" in s) and s.strip().lower().startswith("http")

def expand_maps_url(url):
    """Follow redirects on a Google Maps share link and return the final URL."""
    url = url.strip()
    if url in _expand_cache:
        return _expand_cache[url]
    final_url = url
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            final_url = resp.geturl()
    except Exception as e:
        print(f"Failed expanding map link: {e}")
    _expand_cache[url] = final_url
    return final_url

def _to_lonlat(val1, val2):
    lat = val1 if val1 > 0 else val2
    lon = val2 if val2 < 0 else val1
    return [round(lon, 6), round(lat, 6)]

def extract_coords_from_google_maps_url(input_str):
    if not input_str:
        return None
    input_str = input_str.strip()

    # 1. Plain "lat, lon" pair only. A full URL is handled below, because the first
    # decimals in a URL are the map viewport center, not the venue pin.
    pair = re.fullmatch(r'([-+]?\d+\.\d+)\s*[,\s]\s*([-+]?\d+\.\d+)', input_str)
    if pair:
        return _to_lonlat(float(pair.group(1)), float(pair.group(2)))

    # 2. Expand Google Maps short links or resolve share URLs
    final_url = expand_maps_url(input_str) if is_maps_link(input_str) else input_str

    # 3. Google's protobuf data strings (!3d<lat>!4d<lon>) are the actual place pin
    match_proto = re.search(r'!3d([-+]?\d+\.\d+)!4d([-+]?\d+\.\d+)', final_url)
    if match_proto:
        return [round(float(match_proto.group(2)), 6), round(float(match_proto.group(1)), 6)]

    # 4. @lat,lon (viewport center, usually close to the pin) or ?q=lat,lon / query=lat,lon
    match = re.search(r'(?:@|[?&]q=|[?&]query=)([-+]?\d+\.\d+),([-+]?\d+\.\d+)', final_url)
    if match:
        return _to_lonlat(float(match.group(1)), float(match.group(2)))

    # 5. Coordinates in expanded URL path segments: /41.712331,-88.205216
    match_path = re.search(r'/(-?\d+\.\d+),(-?\d+\.\d+)', final_url)
    if match_path:
        val1, val2 = float(match_path.group(1)), float(match_path.group(2))
        lat = val1 if abs(val1) <= 90 else val2
        lon = val2 if abs(val2) <= 180 else val1
        if abs(lat) <= 90 and abs(lon) <= 180:
            return [round(lon, 6), round(lat, 6)]

    return None

def place_text_from_maps_url(input_str):
    """Pull the place name/address Google embeds in /maps/place/<text>/ share URLs."""
    if not is_maps_link(input_str):
        return None
    final_url = expand_maps_url(input_str)
    m = re.search(r'/maps/place/([^/@?]+)', final_url)
    if m:
        return urllib.parse.unquote_plus(m.group(1)).strip()
    m = re.search(r'[?&](?:q|query)=([^&]+)', final_url)
    if m:
        text = urllib.parse.unquote_plus(m.group(1)).strip()
        if not re.fullmatch(r'[-+\d.,\s]+', text):
            return text
    return None

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

def normalize_address(address):
    """Make a street address friendlier to OpenStreetMap: drop suite numbers and spell out route names."""
    text = address.strip()
    text = re.sub(r'\s*(?:#\s*\w+|\b(?:suite|ste|unit|apt|bldg)\.?\s*\w+)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(?:(?:Illinois|IL)\s+)?(?:Rte|Rt|Route)\.?\s+(\d{1,3})\b', r'Illinois Route \1', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(?:IL|Illinois)[\s-]+(\d{1,3})\b', r'Illinois Route \1', text, flags=re.IGNORECASE)
    text = re.sub(r'\b[NSEW]\.?\s+(?=Illinois Route)', '', text)
    return re.sub(r'\s+', ' ', text).strip(' ,')

def strip_business_name(text):
    """'Shahirizada Restaurant, 3124 IL-59, Naperville' -> '3124 IL-59, Naperville'."""
    m = re.match(r'^[^,\d]*,\s*(\d.*)$', text)
    return m.group(1) if m else text

# OSM classes that mean a specific building/business/address, not a road or area
POINT_CLASSES = {"place", "amenity", "shop", "building", "office", "tourism", "leisure", "craft", "healthcare"}
# Loose bounding box around Naperville / DuPage / Will counties, to reject far-off matches
BBOX = {"lat": (41.55, 41.95), "lon": (-88.45, -87.95)}

def geocode_via_nominatim(address):
    """Geocode with OpenStreetMap Nominatim. Returns [lon, lat] for an address/business point, else None."""
    query = normalize_address(address)
    params = urllib.parse.urlencode({"q": query, "format": "json", "limit": 10, "countrycodes": "us"})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "date-nite-venue-importer (github.com/dp26s22pbm-dev/date-nite)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            results = json.load(resp)
    except Exception as e:
        print(f"Nominatim geocoding error: {e}")
        return None
    for r in results:
        lat, lon = float(r["lat"]), float(r["lon"])
        in_box = BBOX["lat"][0] <= lat <= BBOX["lat"][1] and BBOX["lon"][0] <= lon <= BBOX["lon"][1]
        if r.get("class") in POINT_CLASSES and in_box:
            print(f"Nominatim matched '{query}' -> {r.get('display_name')}")
            return [round(lon, 6), round(lat, 6)]
    print(f"Nominatim: no building/address-level match for '{query}' (road-only and out-of-area results are rejected)")
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

def fetch_pdf_pricing(url):
    """Download a PDF menu and parse pricing with Gemini."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            pdf_bytes = res.read()
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
        print(f"PDF fetch/parse error: {e}")
        return None

def main():
    issue_body = os.environ.get("ISSUE_BODY", "")

    name = extract_field(issue_body, "Restaurant Name")
    category = extract_field(issue_body, "Cuisine Category")
    address = extract_field(issue_body, "Street Address or City")
    coordinates_input = extract_field(issue_body, "Coordinates or Google Maps Link (Optional)")
    menu_url = extract_field(issue_body, "Online Menu / Ordering URL")
    manual_pricing_text = extract_field(issue_body, "Manual Pricing Overrides (Optional)")

    # Extract exact coordinates: try the Coordinates field first, then the address field
    coords = extract_coords_from_google_maps_url(coordinates_input) or extract_coords_from_google_maps_url(address)
    if not coords:
        # Fallback: geocode text. A pasted Maps share link carries a place name/address;
        # try that first, then the street address.
        candidates = []
        for field in (coordinates_input, address):
            text = place_text_from_maps_url(field)
            if text:
                candidates.extend(dict.fromkeys([strip_business_name(text), text]))
        if address and not is_maps_link(address):
            candidates.append(address)
        for text in candidates:
            print(f"No exact coordinates in input; geocoding text via Nominatim: {text}")
            coords = geocode_via_nominatim(text)
            if coords:
                print("WARNING: coordinates are address-level and approximate. "
                      "Paste a Google Maps share link or exact coordinates for storefront precision.")
                break
    if not coords:
        print(f"ABORT: Could not determine coordinates from the Coordinates field ('{coordinates_input}') or the address field ('{address}'). "
              "Paste a Google Maps share link or 'lat, lon' coordinates.")
        return

    # Step 1: Try direct PDF download if the menu URL is a PDF
    pricing = None
    if menu_url and menu_url.lower().endswith(".pdf"):
        pricing = fetch_pdf_pricing(menu_url)

    # Step 2: Use Gemini Google Search Grounding as the primary method
    if not pricing:
        pricing = fetch_pricing_via_gemini_search(name)

    # Parse manual pricing overrides (e.g. "app: 10.00\ncasualMain: 18.00")
    overrides = {}
    if manual_pricing_text:
        for line in manual_pricing_text.strip().splitlines():
            m = re.match(r'\s*(\w+)\s*:\s*\$?\s*([\d.]+)\s*', line)
            if m:
                overrides[m.group(1)] = float(m.group(2))

    # Calculate medians with guarded fallbacks
    if pricing and pricing.get("casualMains"):
        casual_list = pricing.get("casualMains") or [19.00]
        casual_main = round(statistics.median(casual_list), 2)

        prem_list = pricing.get("premiumMains") or [round(casual_main * 1.6, 2)]
        premium_main = round(statistics.median(prem_list), 2)

        apps_list = pricing.get("apps") or [round(casual_main * 0.55, 2)]
        app = round(statistics.median(apps_list), 2)

        drinks_list = pricing.get("drinks") or [8.00]
        drink = round(statistics.median(drinks_list), 2)

        desserts_list = pricing.get("desserts") or [8.00]
        dessert = round(statistics.median(desserts_list), 2)
    else:
        # Fallback to realistic Naperville full-service medians if search fails
        casual_main = 19.00
        premium_main = 34.00
        app = 12.00
        drink = 8.50
        dessert = 8.00

    # Apply manual pricing overrides if provided
    if overrides:
        if "app" in overrides: app = overrides["app"]
        if "casualMain" in overrides: casual_main = overrides["casualMain"]
        if "premiumMain" in overrides: premium_main = overrides["premiumMain"]
        if "drink" in overrides: drink = overrides["drink"]
        if "dessert" in overrides: dessert = overrides["dessert"]

    total_cost = round((app + (1 * casual_main) + (1 * premium_main) + (2 * drink) + dessert) * 1.30)
    tier_color = calculate_tier_color(total_cost)

    with open("venues.json", "r") as f:
        venues = json.load(f)

    next_id = max([v.get("id", 0) for v in venues], default=0) + 1

    # Overwrite if restaurant already exists, else append
    existing_idx = next((i for i, v in enumerate(venues) if v["name"].lower() == name.lower()), None)
    entry = {
        "id": (existing_idx + 1) if existing_idx is not None else next_id,
        "name": name,
        "category": category,
        "coords": coords,
        "app": app,
        "casualMain": casual_main,
        "premiumMain": premium_main,
        "drink": drink,
        "dessert": dessert,
        "tierColor": tier_color
    }

    if existing_idx is not None:
        venues[existing_idx] = entry
    else:
        venues.append(entry)

    with open("venues.json", "w") as f:
        json.dump(venues, f, indent=2)

    # Register URL into urls.py so future batch runs can refresh it
    if menu_url:
        try:
            with open("urls.py", "r") as f:
                urls_content = f.read()

            # Find the last closing brace and insert the new key-value pair right before it
            last_brace_idx = urls_content.rfind("}")
            if last_brace_idx != -1:
                new_entry_line = f'    {entry["id"]}: "{menu_url}",\n'
                updated_urls = urls_content[:last_brace_idx] + new_entry_line + urls_content[last_brace_idx:]
                with open("urls.py", "w") as f:
                    f.write(updated_urls)
        except Exception as e:
            print(f"Warning: Could not append URL to urls.py: {e}")

    print(f"Added {name} (ID: {entry['id']}): Casual ${casual_main}, Prime ${premium_main}, Color: {tier_color}")

if __name__ == "__main__":
    main()
