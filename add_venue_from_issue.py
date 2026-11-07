import os
import re
import sys
import time
import json
import math
import ssl
import statistics
import urllib.request
import urllib.parse
from html import unescape
import places_usage
import menu_log
import pricing_rules
from google import genai
from google.genai import types

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

# Scope rules shared by every pricing prompt. Each one closes a way the wrong menu has been read before:
# catering trays, lunch-only prices, fixed-price packages and invented numbers.
SCOPE_RULES = """Scope rules (they apply to every category below):
- Use dinner (or all-day dine-in) prices for one person. Ignore lunch, brunch, happy hour, kids, party or group packages, and catering.
- Catering prices are per tray, pan or "serves N" and run several times a dine-in price. If the only prices you can find are catering prices, return empty lists.
- A fixed price per person (a buffet, a rodizio, a prix fixe) is one main. Use its dinner price, not the lunch price.
- drinks: priced cocktails or craft beverages. Never include soft drinks, coffee, tea, juice or other non-alcoholic drinks. Use beer and wine only if no cocktail is priced. Many menus list no alcohol prices; then return an empty list.
- If a category has no prices printed on the menu, return an empty list for it. Never estimate or fill in a typical price.
- This map lists sit-down restaurants only. If any page you find shows the place is a food truck, trailer, cart, stand, pop-up or other mobile or counter-only vendor (photos of a truck, "food truck" in a listing or description, no dining room), return empty lists for every category, even when a menu with prices exists.
"""

SEARCH_PRICING_PROMPT = """
Search for current menu pricing for "{venue_name}" in {city}.
Find real-world pricing for these categories:
1. apps: typical appetizer / starter item price (in USD, number only)
2. casualMains: standard, lower-cost main entree or sandwich prices (in USD, number only)
3. premiumMains: prices of the steak and expensive seafood entrees (for example filet, ribeye, strip, lobster, king crab, scallops, salmon, sea bass), in USD, number only. List the standard single-entree plate for each. Do not include chicken, pork or vegetarian dishes, combination plates or multi-protein platters. Exclude large-format or shared cuts (tomahawk, porterhouse for two), wagyu, market-price or "MP" items, surf-and-turf add-ons, seafood towers and platters for sharing. If the menu has no steak or expensive seafood, list its highest priced standard entrees instead.
4. drinks: standard cocktail or craft beverage prices (in USD, number only)
5. desserts: typical dessert prices (in USD, number only)

{scope_rules}
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
- Prices may have no dollar sign and may run into a calorie count: "12.991,220 cal" is $12.99 and 1,220 cal. Calorie counts are never prices.
- A price can be a plain number after the dish name with no dollar sign ("grilled dorade 49" is $49).
- "apps": Starters, sides, appetizers, soups, salads.
- "casualMains": Standard pastas, pizzas, handhelds, burgers, chicken entrees, or basic sandwiches.
- "premiumMains": Standard steaks (ribeye, strip, filet), veal chops, prime seafood (salmon, lobster, sea bass), with the standard single-entree plate for each. Exclude combination or multi-protein plates, chicken and vegetarian dishes, large-format or shared cuts, wagyu, market-price items and sharing platters. If there are no premium luxury cuts, use the top 25% highest priced entrees.
- "drinks": Specialty cocktails; if none are priced, house wine or draft beer. Never soft drinks or other non-alcoholic drinks.
- "desserts": Tiramisu, cannoli, cakes, gelato.
""" + SCOPE_RULES + "Return strictly valid JSON with no markdown formatting.\n"

def extract_field(body, header):
    pattern = rf"### {re.escape(header)}\s*\n\s*(.*?)(?=\n###|\Z)"
    match = re.search(pattern, body, re.DOTALL)
    value = match.group(1).strip() if match else ""
    # GitHub issue forms put this placeholder in every optional field the user leaves blank
    return "" if value.lower() == "_no response_" else value

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

def normalize_website(value):
    """Return a clean http(s) URL, or "" if the field is blank or not a usable web address."""
    value = (value or "").strip()
    if not value:
        return ""
    value = value.split()[0]
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        print(f"Warning: ignoring website that is not a valid web address: {value}")
        return ""
    return value

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

def decode_maps_text(raw):
    """Decode a Maps URL fragment. Google writes a space as '+', and some links also write a literal plus sign
    unencoded, so 'Sushi + Grill' arrives as 'Sushi+++Grill'. Treat a run of exactly three as ' + '."""
    raw = re.sub(r"(?<=[^+])\+\+\+(?=[^+])", " %2B ", raw)
    return urllib.parse.unquote_plus(raw).strip()

def place_text_from_maps_url(input_str):
    """Pull the place name/address Google embeds in /maps/place/<text>/ share URLs."""
    if not is_maps_link(input_str):
        return None
    final_url = expand_maps_url(input_str)
    m = re.search(r'/maps/place/([^/@?]+)', final_url)
    if m:
        return decode_maps_text(m.group(1))
    m = re.search(r'[?&](?:q|query)=([^&]+)', final_url)
    if m:
        text = decode_maps_text(m.group(1))
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
    # Google sometimes runs the street number into the direction ("3124S Rte 59")
    text = re.sub(r'\b(\d+)([NSEW])\b(?=\s)', r'\1 \2', text)
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

def reverse_geocode_town(coords):
    """Town and two-letter state for [lon, lat] via Nominatim, for the line under the cuisine on the popup. ("", "") on failure."""
    params = urllib.parse.urlencode({"format": "jsonv2", "zoom": 14, "addressdetails": 1, "lat": coords[1], "lon": coords[0]})
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/reverse?{params}",
        headers={"User-Agent": "date-nite-venue-importer (github.com/dp26s22pbm-dev/date-nite)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            addr = json.load(resp).get("address", {})
    except Exception as e:
        print(f"Nominatim reverse geocoding error: {e}")
        return "", ""
    town = next((addr[k] for k in ("city", "town", "village", "hamlet", "municipality") if addr.get(k)), "")
    iso = addr.get("ISO3166-2-lvl4", "")
    return town, (iso.split("-")[1] if iso.startswith("US-") else "")

_ADDRESS_TOWN = re.compile(r",\s*([^,\d]+?),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*(?:USA|United States))?\s*$")

def town_from_address(address):
    """Town and two-letter state from a US address such as '51 Town Square, Wheaton, IL 60189, USA', the way Google
    prints it. This is the postal town, so it also covers unincorporated areas the coordinate lookup has no name for.
    ("", "") when the text does not end in a town, state and optional zip."""
    m = _ADDRESS_TOWN.search((address or "").strip())
    return (m.group(1).strip(), m.group(2)) if m else ("", "")

# (title, url) of the pages the last web search consulted, shown in the report so the estimates can be checked by hand
search_sources = []

def grounding_sources(response, limit=5):
    """The pages Gemini's search grounding consulted, as unique (title, url) pairs. These are the pages it read, not
    proof of where each figure came from, and the urls are often Google redirect links that open the page."""
    found = []
    for cand in getattr(response, "candidates", None) or []:
        meta = getattr(cand, "grounding_metadata", None)
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web and web.uri and web.uri not in [u for _, u in found]:
                found.append((web.title or web.uri, web.uri))
    return found[:limit]

def grounding_summary(response):
    """What Google's search grounding reported, for the job log: the queries Gemini ran and how many source pages came
    back. No queries means Gemini did not search at all."""
    queries, chunks = [], 0
    for cand in getattr(response, "candidates", None) or []:
        meta = getattr(cand, "grounding_metadata", None)
        if meta is None:
            return "no grounding metadata in the response"
        queries += list(getattr(meta, "web_search_queries", None) or [])
        chunks += len(getattr(meta, "grounding_chunks", None) or [])
    return f"search queries={queries} source pages={chunks}"

# True when the last web search returned no source pages even after a retry, so its prices may come from Gemini's memory
search_ungrounded = False
# True when Claude's web search supplied the prices because Gemini's search had no sources or no prices
search_by_claude = False
# Set when Gemini had no sources and Claude gave no usable sourced prices, so the report can say what happened:
# "ran" (Claude searched and found nothing; claude_found_nothing_pages is how many pages it consulted) or "not run"
# (no API key, or the request failed before it searched). None means Claude was not needed.
claude_backup_result = None
claude_found_nothing_pages = 0

def fetch_pricing_via_gemini_search(venue_name, city="Naperville, IL"):
    """Query Gemini with Google Search Grounding to find live menu pricing. A response with no source pages means
    Gemini may not have searched, so it is asked once more; if that is empty too, search_ungrounded is set."""
    global search_ungrounded
    search_ungrounded = False
    search_sources.clear()
    try:
        prompt = SEARCH_PRICING_PROMPT.format(venue_name=venue_name, city=city, scope_rules=SCOPE_RULES)
        retry_note = ("\nYou must run a Google Search for this restaurant's menu and base the prices on the pages you find. "
                      "Do not answer from memory.\n")
        for attempt in (1, 2):
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt + (retry_note if attempt == 2 else ""),
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1
                ),
            )
            search_sources[:] = grounding_sources(response)
            print(f"Web search for {venue_name} (attempt {attempt}): {grounding_summary(response)}")
            if search_sources:
                break
        search_ungrounded = not search_sources
        if search_ungrounded:
            menu_log.step("search_gemini", "no_sources", detail=grounding_summary(response))
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`").removeprefix("json").strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"Gemini Search Grounding error for {venue_name}: {e}")
        menu_log.step("search_gemini", "error", detail=e)
        return None

CLAUDE_SEARCH_MODEL = os.environ.get("CLAUDE_SEARCH_MODEL", "claude-sonnet-5-5")
# Caps on the Claude fallback. The log showed it consulting 9 to 23 pages per venue, mostly aggregator listings, and
# still returning defaulted categories about half the time, so searches, fetches and continuation passes are kept low.
CLAUDE_MAX_SEARCHES = 1
CLAUDE_MAX_FETCHES = 1
CLAUDE_MAX_PASSES = 2

def claude_search_pricing(venue_name, city, website=""):
    """Backup for Gemini's web search: ask Claude, with Anthropic's web search and web fetch tools, for the same pricing
    JSON. Returns (pricing, sources) where sources is a list of (title, url) for the pages it searched or fetched, or
    (None, []) when there is no key, the request fails, or no prices come back."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is not set; skipping the Claude web search.")
        menu_log.step("search_claude", "no_key")
        return None, []
    try:
        import anthropic
        claude = anthropic.Anthropic(api_key=api_key)
        prompt = SEARCH_PRICING_PROMPT.format(venue_name=venue_name, city=city, scope_rules=SCOPE_RULES)
        prompt += ("\nSearch the web for this restaurant's own menu, or a menu page from an ordering platform that shows its "
                   "prices. Use only dish prices printed on a menu page you found, never a typical price you remember. "
                   "If you find no real dish prices, return empty lists. Also check what kind of place this is: read the listing "
                   "or review pages you find, and if any of them shows a food truck or other mobile vendor, return empty lists.\n")
        if website:
            prompt += f"The restaurant's website is {website}. Fetch its menu pages if they show prices.\n"
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": CLAUDE_MAX_SEARCHES},
                 {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": CLAUDE_MAX_FETCHES}]
        messages = [{"role": "user", "content": prompt}]
        sources = []  # every page that came back, search hits included
        read = []  # only the pages Claude fetched or cited, which are the ones it can have taken prices from
        tokens_in = tokens_out = 0
        for _ in range(CLAUDE_MAX_PASSES):  # a server-side tool loop that runs long comes back paused; send it back to continue
            response = claude.messages.create(model=CLAUDE_SEARCH_MODEL, max_tokens=8000, tools=tools, messages=messages)
            usage = getattr(response, "usage", None)
            tokens_in += (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0) \
                + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            tokens_out += getattr(usage, "output_tokens", 0) or 0
            for block in response.content:
                found = []
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    found = [(getattr(r, "title", None), getattr(r, "url", None)) for r in block.content]
                elif block.type == "web_fetch_tool_result":
                    found = [(None, getattr(block.content, "url", None))]
                elif block.type == "text":
                    found = [(getattr(c, "title", None), getattr(c, "url", None)) for c in getattr(block, "citations", None) or []]
                for title, url in found:
                    if url and url not in [u for _, u in sources]:
                        sources.append((title or url, url))
                    if url and block.type != "web_search_tool_result" and url not in [u for _, u in read]:
                        read.append((title or url, url))
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
        print(f"Claude web search for {venue_name}: stop_reason={response.stop_reason} source pages={len(sources)}")
        claude_pages[0] = len(sources)
        claude_read[0] = bool(read)
        claude_result_urls[:] = [u for _, u in sources]
        if response.stop_reason == "refusal":
            print(f"Claude web search for {venue_name} was refused, so there are no prices from it.")
            menu_log.step("search_claude", "refused", pages=len(sources), tokens_in=tokens_in, tokens_out=tokens_out)
            return None, []
        text = "".join(b.text for b in response.content if b.type == "text")
        start, end = text.find("{"), text.rfind("}")
        pricing = clean_pricing(json.loads(text[start:end + 1])) if start != -1 and end > start else None
        if not pricing:
            # Without this the log cannot say whether Claude found no prices or answered in a form the parser rejected
            print(f"Claude's reply for {venue_name} had no usable prices. Its reply was: {text[:1500]!r}")
        menu_log.step("search_claude", "ok" if pricing else "no_prices", pages=len(sources), read=len(read),
                      tokens_in=tokens_in, tokens_out=tokens_out,
                      sources=[menu_log.host_of(u) for _, u in sources[:5]] or None)
        # The pages Claude opened or cited come first, then the remaining search hits, so the report can name what was read
        return pricing, (read + [x for x in sources if x not in read])[:5]
    except Exception as e:
        print(f"Claude web search error for {venue_name}: {e}")
        menu_log.step("search_claude", "error", detail=e)
        return None, []

claude_pages = [None]  # pages the last Claude search consulted; None until a search ran. A list so claude_search_pricing can set it
claude_result_urls = []  # every URL the last Claude search saw, search hits included, for the Yelp menu lookup below
claude_read = [False]  # True when the last Claude search fetched or cited at least one page, False when it only saw search results

# Set when the web search step found the venue's own menu (the Yelp menu page) instead of estimates, so the caller labels
# the prices by that source and does not mark them as guesses. "" otherwise.
web_search_menu_source = ""

_YELP_SLUG = re.compile(r"https?://(?:www\.)?yelp\.com/(?:biz|biz_photos|menu)/([A-Za-z0-9_%\-]+)", re.I)

def yelp_menu_url(urls):
    """The Yelp menu page for the venue behind any Yelp business, photo or menu link in urls, or None. Yelp lists a
    restaurant's full menu with prices at yelp.com/menu/<same slug as the business page>."""
    for u in urls or []:
        m = _YELP_SLUG.match(u or "")
        if m and m.group(1).lower() not in ("search", "c"):
            return f"https://www.yelp.com/menu/{m.group(1)}"
    return None

def pricing_from_yelp_menu(venue_name, urls):
    """Prices read from the venue's Yelp menu page when one of urls (the pages a web search found) points at its Yelp
    listing, else None. The page must name the venue, so a same-named place elsewhere is not used. Yelp can turn a
    script away; fetch_url logs that and this returns None, leaving the estimates as they were."""
    url = yelp_menu_url(urls)
    if not url:
        return None
    got = fetch_url(url)
    if not got or "html" not in got[1].lower():
        menu_log.step("yelp_menu", "not_fetched", url=url)
        return None
    text = html_to_text(got[0].decode("utf-8", "ignore"))
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    if squash(venue_name) not in squash(text[:300]):
        menu_log.step("yelp_menu", "not_this_venue", url=url)
        return None
    return pricing_from_menu_text(text, where="yelp_menu", url=url)

def fetch_pricing_via_web_search(venue_name, city, website=""):
    """Gemini's web search first. When it returns no source pages (so its prices may be from memory) or no prices, Claude
    searches instead, and its prices are used only when it found source pages. If Gemini had no sources and Claude gives no
    sourced prices either, nothing is returned: Gemini's unsourced prices are not kept, so the venue counts as having no
    prices found (the cell and city imports skip it, and the Add Venue form asks for the menu)."""
    global search_ungrounded, search_by_claude, claude_backup_result, claude_found_nothing_pages, web_search_menu_source
    web_search_menu_source = ""
    claude_result_urls.clear()
    search_by_claude = False
    claude_backup_result = None
    claude_found_nothing_pages = 0
    claude_pages[0] = None
    claude_read[0] = False
    pricing = fetch_pricing_via_gemini_search(venue_name, city)
    if search_ungrounded or not pricing:
        print("Gemini's web search had no usable sourced prices; asking Claude to search.")
        claude_pricing, sources = claude_search_pricing(venue_name, city, website)
        # Claude's search often turns up the venue's Yelp listing, whose menu page has the real prices
        yelp_pricing = pricing_from_yelp_menu(venue_name, claude_result_urls)
        if yelp_pricing:
            print("Read the venue's Yelp menu page instead of using search estimates.")
            web_search_menu_source = "the venue's Yelp menu page"
            search_ungrounded = False
            search_sources.clear()
            return yelp_pricing
        if claude_pricing and sources:
            pricing = claude_pricing
            search_sources[:] = sources
            search_ungrounded = False
            search_by_claude = True
        elif search_ungrounded:
            print("Claude found no sourced prices either, so Gemini's unsourced prices are being dropped.")
            pricing = None
            search_sources.clear()
            claude_backup_result = "not run" if claude_pages[0] is None else "ran"
            claude_found_nothing_pages = claude_pages[0] or 0
    return pricing

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

def menu_file_mime(url, content_type=""):
    """"application/pdf" or an image type when a URL is a PDF or an image menu, else None."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if "pdf" in ctype or urllib.parse.urlparse(url).path.lower().endswith(".pdf"):
        return "application/pdf"
    if ctype in IMAGE_TYPES.values():
        return ctype
    return IMAGE_TYPES.get(os.path.splitext(urllib.parse.urlparse(url).path.lower())[1])

def fetch_pdf_pricing(url, mime="application/pdf"):
    """Download a PDF or image menu and parse pricing with Gemini."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            pdf_bytes = res.read(20000000)
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[
                PDF_PROMPT,
                types.Part.from_bytes(data=pdf_bytes, mime_type=mime)
            ]
        )
        clean = response.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(clean)
        found = clean_pricing(data)
        menu_log.step("pdf", "ok" if found else "no_prices", url=url, mime=mime,
                      n=sum(len(v) for v in found.values()) if found else 0)
        return data
    except Exception as e:
        print(f"PDF or image fetch/parse error: {e}")
        menu_log.step("pdf", "download_or_parse_error", url=url, detail=e, mime=mime)
        return None

PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{2})?")
# Some menu pages copy as "12.991,220 cal": no dollar sign, and the price runs into the calorie count
BARE_PRICE_RE = re.compile(r"(?<![\d.,])\d{1,3}\.\d{2}")
# Menus drawn by a widget often show "grilled dorade<tab>49": a whole-dollar price with no dollar sign
TAB_PRICE_RE = re.compile(r"(?m)\S[^\n]*\t\d{1,3}(?:\.\d{2})?[ \t]*$")
# Menus that print a price alone on the line under the dish name: "Edamame<newline>9<newline>Steamed soy beans"
# (Nabuki). No dollar sign, decimal or tab, so the patterns above miss them. A stray number can match too (a page
# number), which is why six are required and Gemini makes the final call on what is a price.
LINE_PRICE_RE = re.compile(r"(?m)^[ \t]*\d{1,3}(?:\.\d{2})?[ \t]*$")
MENU_TEXT_LIMIT = 60000

def price_count(text):
    """How many price-like numbers the text has, by whichever price style it uses most."""
    return max(len(r.findall(text)) for r in (PRICE_RE, BARE_PRICE_RE, TAB_PRICE_RE, LINE_PRICE_RE))

def clean_pricing(data, require_main=True):
    """Keep only sane numbers from a Gemini pricing reply. Returns None unless it has at least one casual main, or, with
    require_main=False (a partial menu such as a dessert-only page), unless it has a price in any category."""
    if not isinstance(data, dict):
        return None
    out = {}
    for key in ("apps", "casualMains", "premiumMains", "drinks", "desserts"):
        values = []
        for x in data.get(key) or []:
            try:
                f = float(x)
            except (TypeError, ValueError):
                continue
            if 0 < f < 500:
                values.append(round(f, 2))
        out[key] = values
    if require_main:
        return out if out["casualMains"] else None
    return out if any(out.values()) else None

def pricing_from_menu_text(text, require_main=True, where="pasted_text", url=None):
    """Parse menu prices out of plain text (a pasted menu or a restaurant's menu page) with Gemini. With
    require_main=False a partial menu (desserts only, say) is returned with the other categories empty."""
    text = (text or "").strip()
    # where: "pasted_text", "page_html" (plain request) or "page_browser" (rendered), for the menu log
    seen = {"url": url, "chars": len(text), "platform": menu_log.platform_of(url or "", text) if url else None}
    # Image and other binary bytes decoded as text contain digit patterns that look like prices
    if "\x00" in text or sum(1 for c in text[:5000] if c == "\ufffd" or (ord(c) < 32 and c not in "\n\r\t")) > 50:
        print("Menu text looks like binary data; not using it.")
        menu_log.step(where, "binary", **seen)
        return None
    prices = price_count(text)
    if prices < 6:
        print("Menu text has too few prices to use.")
        menu_log.step(where, "too_few_prices", n=prices, **seen)
        return None
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[PDF_PROMPT, "Menu text:\n" + text[:MENU_TEXT_LIMIT]],
        )
        clean = response.text.strip().replace("```json", "").replace("```", "")
        pricing = clean_pricing(json.loads(clean), require_main)
        menu_log.step(where, "ok" if pricing else "no_casual_main", n=prices, **seen)
        return pricing
    except Exception as e:
        print(f"Menu text parse error: {e}")
        menu_log.step(where, "gemini_error", detail=e, n=prices, **seen)
        return None

# Hosts that refused a plain request (401, 403, 429) or dropped the connection during the TLS handshake. A real browser
# often gets through where a script is turned away.
BLOCKED_HOSTS = set()
# Hosts whose TLS handshake failed once; plain requests to them are skipped (the same failure would repeat for every
# guessed menu path) and the browser reads them instead
TLS_FAILED_HOSTS = set()
# Hosts that answered with a bot check ("Just a moment..." and similar) instead of the page, in a plain request and then
# in the browser. Every other path on the host gets the same check, so the importer stops guessing pages there and
# moves to the next source. Nothing here tries to pass the check.
CHALLENGED_HOSTS = set()
# Plain requests: a challenge arrives as a 403 with this header. Other bot walls are recognized by their page text.
BOT_CHALLENGE_TITLE_RE = re.compile(r"<title[^>]*>\s*(just a moment|attention required|checking your browser|pardon our interruption|verify you are human)", re.I)
BOT_CHALLENGE_TEXT_RE = re.compile(r"verify you are (a )?human|checking if the site connection is secure|enable javascript and cookies to continue|press (&|&amp;|and) hold|request unsuccessful\. incapsula|captcha-delivery\.com|px-captcha", re.I)

def looks_like_bot_challenge(html, headers=None):
    """True when a response is a bot check and not the page. Cloudflare says so in a cf-mitigated header; any bot wall
    shows a short page (a real page that merely loads a captcha script has far more text) or a known title."""
    if headers is not None and "challenge" in (headers.get("cf-mitigated") or "").lower():
        return True
    if not html:
        return False
    if BOT_CHALLENGE_TITLE_RE.search(html[:5000]):
        return True
    return len(html_to_text(html)) < 1500 and bool(BOT_CHALLENGE_TEXT_RE.search(html) or "challenges.cloudflare.com" in html)

# Some sites set a cookie and redirect; without a cookie jar a script is bounced in a loop
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

def _host(url):
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")

def fetch_url(url, limit=2000000, with_url=False):
    """GET a page. Returns (bytes, content_type), or (bytes, content_type, final_url) with with_url, or None when it
    is blocked or fails."""
    if _host(url) in TLS_FAILED_HOSTS:
        menu_log.step("fetch", "skipped_tls_host", url=url)
        return None
    if _host(url) in CHALLENGED_HOSTS:
        return None  # already logged as bot_challenge; the same check would answer every path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with _opener.open(req, timeout=15) as resp:
            if resp.status != 200:
                menu_log.step("fetch", f"http_{resp.status}", url=url)
                return None
            data, ctype = resp.read(limit), resp.headers.get("Content-Type", "")
            return (data, ctype, resp.geturl()) if with_url else (data, ctype)
    except urllib.error.HTTPError as e:
        # 301 to 308 here means urllib gave up on a redirect loop, which a browser may follow
        if e.code in (401, 403, 429, 301, 302, 303, 307, 308):
            BLOCKED_HOSTS.add(_host(url))
        print(f"Could not fetch {url}: {e}")
        challenged = False
        if e.code in (403, 429, 503):
            try:
                challenged = looks_like_bot_challenge(e.read(50000).decode("utf-8", "ignore"), e.headers)
            except Exception:
                pass
        menu_log.step("fetch", "bot_challenge" if challenged else f"http_{e.code}", url=url, detail=f"http_{e.code}" if challenged else None)
        return None
    except Exception as e:
        # A handshake or connection the server drops on a script (Hinsdale Prime ends every urllib TLS handshake with
        # an unexpected EOF) may still work in a browser
        if isinstance(e, ssl.SSLError) or isinstance(getattr(e, "reason", None), (ssl.SSLError, ConnectionError)) \
                or isinstance(e, ConnectionError):
            BLOCKED_HOSTS.add(_host(url))
            TLS_FAILED_HOSTS.add(_host(url))
            print(f"Could not fetch {url}: {e}; this site will be read through a browser.")
            menu_log.step("fetch", "tls_failed" if isinstance(e, ssl.SSLError) or isinstance(getattr(e, "reason", None), ssl.SSLError) else "connection_failed", url=url, detail=e)
            return None
        print(f"Could not fetch {url}: {e}")
        menu_log.step("fetch", "fetch_error", url=url, detail=e)
        return None

def html_to_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(html)).strip()

# Tabs that hold the dining-room food and drink menus. Lunch, brunch and happy hour tabs are read only when a page
# has nothing else, because SCOPE_RULES says to ignore them.
TAB_KEEP_WORDS = ("dinner", "all day", "all-day", "food", "dessert", "dine", "beverage", "drink", "cocktail", "wine", "beer", "menu")
TAB_SKIP_WORDS = ("lunch", "brunch", "happy", "kid", "catering", "party", "private", "gift")

# Each browser launch costs 10 to 45 seconds, so a run only opens a few
BROWSER_LIMIT = 12
browser_launches = 0
# url -> (html, final_url) of pages the browser has read
RENDERED_HTML = {}

def _use_browser():
    """True when a browser may be launched now: playwright is installed and the run's limit is not spent."""
    global browser_launches
    if sync_playwright is None:
        print("playwright is not installed; skipping the browser read.")
        menu_log.step("browser", "unavailable")
        return False
    if browser_launches >= BROWSER_LIMIT:
        print("Browser limit reached for this run; skipping the browser read.")
        menu_log.step("browser", "limit")
        return False
    browser_launches += 1
    return True

def _open_page(page, url, timeout_ms=45000):
    """Load a page and give its scripts time to draw. A page that never goes quiet (trackers, chat widgets) is still
    used once it has loaded."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

def _scroll_page(page):
    """Scroll to the bottom in steps. Many sites draw a section (including the menu link and dish lists) only when it
    scrolls into view."""
    try:
        for _ in range(15):
            height = page.evaluate("document.body.scrollHeight")
            page.evaluate("window.scrollBy(0, window.innerHeight)")
            page.wait_for_timeout(400)
            at_bottom = page.evaluate("window.scrollY + window.innerHeight >= document.body.scrollHeight - 5")
            if at_bottom and page.evaluate("document.body.scrollHeight") == height:
                break
        page.wait_for_timeout(800)
    except Exception:
        pass

MORE_RE = re.compile(r"load more|show all|show more|view all|see more", re.I)

def _expand_page(page):
    """Click "Load more content" and "Show all items" controls so a menu that lists a few dishes at a time shows all
    of them. Links that leave the page are not clicked."""
    for _ in range(6):
        clicked = False
        for control in page.locator("button, [role=button], a").filter(has_text=MORE_RE).all()[:10]:
            try:
                href = control.get_attribute("href")
                if (href and not href.startswith("#")) or not control.is_visible():
                    continue
                control.click(timeout=3000)
                page.wait_for_timeout(1500)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            break

def _note_browser_challenge(url, html):
    """True when the browser was shown a bot check instead of the page. Logs it and marks the host so the importer
    stops opening more pages there."""
    if not looks_like_bot_challenge(html):
        return False
    print(f"{url} answered the browser with a bot check; no more pages on {_host(url)} will be tried.")
    CHALLENGED_HOSTS.add(_host(url))
    menu_log.step("browser", "bot_challenge", url=url, chars=len(html or ""))
    return True

def render_page_html(url):
    """(html, final_url) of a page after a headless browser has loaded it, or None. Used for sites that refuse plain
    requests."""
    if _host(url) in CHALLENGED_HOSTS or not _use_browser():
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                _open_page(page, url)
                _scroll_page(page)
                try:
                    # Sites built with scripts add their links after the page loads; the menu link is the one we want
                    page.wait_for_selector('a[href*="menu" i]', timeout=10000, state="attached")
                except Exception:
                    pass
                html = page.content()
                if _note_browser_challenge(url, html):
                    return None
                print(f"Browser read {url} -> {page.url}: {len(html)} characters of HTML, {len(ANCHOR_RE.findall(html))} links")
                menu_log.step("browser", "ok", url=url, chars=len(html), links=len(ANCHOR_RE.findall(html)),
                              platform=menu_log.platform_of(page.url, html))
                return html, page.url
            finally:
                browser.close()
    except Exception as e:
        print(f"Browser read failed for {url}: {e}")
        menu_log.step("browser", "failed", url=url, detail=e)
        return None

def render_page_text(url, timeout_ms=45000):
    """Open a page in a headless browser so menus that JavaScript draws (including ones split into tabs) have their
    text. Returns the visible text plus each tab's text under a "=== tab ===" heading, or None when playwright is not
    installed or the page will not load."""
    if _host(url) in CHALLENGED_HOSTS or not _use_browser():
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
                _open_page(page, url, timeout_ms)
                _scroll_page(page)
                if _note_browser_challenge(url, page.content()):
                    return None
                _expand_page(page)
                parts = [page.inner_text("body")]
                # Menu widgets (SinglePlatform and similar) draw inside an iframe, which the page's own text leaves out
                for frame in page.frames[1:]:
                    try:
                        framed = frame.inner_text("body")
                    except Exception:
                        continue
                    if len(framed) > 200:
                        parts.append("=== embedded frame ===\n" + framed[:40000])
                tabs = page.locator("[role=tab]")
                labels = [(tabs.nth(i).inner_text() or "").strip() for i in range(min(tabs.count(), 20))]
                wanted = [i for i, t in enumerate(labels) if any(w in t.lower() for w in TAB_KEEP_WORDS) and not any(w in t.lower() for w in TAB_SKIP_WORDS)]
                for i in wanted or [i for i, t in enumerate(labels) if t]:
                    try:
                        tabs.nth(i).click(timeout=5000)
                        page.wait_for_timeout(500)
                        parts.append(f"=== {labels[i]} ===\n" + page.inner_text("body"))
                    except Exception as e:
                        print(f"Could not open the {labels[i]!r} tab on {url}: {e}")
                try:
                    RENDERED_HTML[url] = (page.content(), page.url)
                except Exception:
                    pass
                text = "\n".join(parts)
                print(f"Browser read {url} -> {page.url}: {len(text)} characters of text, {len(PRICE_RE.findall(text))} dollar prices")
                menu_log.step("browser", "ok", url=url, chars=len(text), n=len(PRICE_RE.findall(text)),
                              frames=len(page.frames) - 1 or None, tabs=len(labels) or None,
                              platform=menu_log.platform_of(page.url, RENDERED_HTML.get(url, ("", ""))[0]))
                return text
            finally:
                browser.close()
    except Exception as e:
        print(f"Browser read failed for {url}: {e}")
        menu_log.step("browser", "failed", url=url, detail=e)
        return None

def get_page(url):
    """(html, final_url) for a page: a plain request first, then a browser when the site refuses plain requests."""
    got = fetch_url(url, with_url=True)
    if got:
        return got[0].decode("utf-8", "ignore"), got[2]
    if _host(url) in BLOCKED_HOSTS:
        return render_page_html(url)
    return None

MENU_LINK_WORDS = ("menu", "food", "dine", "dining", "drink", "order", "eat", "lunch", "dinner", "brunch")
ORDERING_HOSTS = ("toasttab.com", "toast.app","chownow.com", "square.site", "olo.com", "popmenu.com", "order.online", "ordering.", "menus.",
                  "orderexperience.net", "alohaorderonline.com")
MENU_PATHS = ("/menu", "/menus", "/our-menu", "/food-menu", "/food", "/dinner", "/dinner-menu", "/dining", "/order", "/order-online")

# Links to these are never the dinner menu. Catering and party menus carry tray prices, so reading one gives prices
# several times too high.
SKIP_MENU_WORDS = ("get.popmenu.com", "levelaccess.com", "/items/", "/item/", "catering", "kids", "kid-", "party", "banquet", "private", "gift", "event")
# A file name often says "lunch" for the restaurant's one menu (NWB's only menu link is nwb_..._lunch_menu_....pdf), so
# these links go to the back of the queue instead of being dropped. The prompt tells Gemini to ignore lunch-only prices.
SOFT_MENU_WORDS = ("brunch", "lunch", "happy")

def menu_link_score(url, label=""):
    """Rank a candidate menu link: dinner menus first, drink-only and lunch-named menus last."""
    text = (url + " " + label).lower()
    return ((3 if "dinner" in text else 0) + (1 if "menu" in text else 0)
            - (2 if any(w in text for w in ("bar", "drink", "wine", "beer", "cocktail")) else 0)
            - (3 if any(w in text for w in SOFT_MENU_WORDS) else 0))

# Chain sites open on a city picker whose menu sits a level down (/chicago/menu). Links to these are not location pages.
LOCATION_SKIP_WORDS = ("site-map", "sitemap", "accessibility", "privacy", "terms", "career", "job", "press", "contact",
                       "about", "gift", "event", "private", "catering", "franchise", "news", "blog", "login")
ANCHOR_RE = re.compile(r"(?is)<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>")

# Popmenu sites (Rock Bottom's among them) keep their nav menus as data in the page and draw a dropdown only on click, so
# the "Order Online" links (one per location) are in a script block and not in any <a> tag.
NAV_LINK_RE = re.compile(r'"__typename":"RestaurantNavLink"[^{}]*?"name":"([^"]*)"[^{}]*?"url":"(https?:[^"]+)"')
# Delivery apps mark prices up, and these are not menus
NON_MENU_HOSTS = ("opentable.com", "resy.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
                  "youtube.com", "linkedin.com", "yelp.com", "tripadvisor.com", "apple.com", "google.com", "doordash.com",
                  "ubereats.com", "grubhub.com", "postmates.com")

def embedded_nav_links(page):
    """(url, label) for external links in a Popmenu-style nav menu held as data in the page, minus social, reservation
    and delivery-app links."""
    found = []
    for name, url in NAV_LINK_RE.findall(page):
        url = url.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002F", "/")
        host = urllib.parse.urlparse(url).netloc.lower()
        label = unescape(name).lower()
        # Loyalty sign-ins and gift cards sit in the same nav; keep ordering platforms and links that say menu or order
        wanted = any(h in host for h in ORDERING_HOSTS) or any(w in (url + " " + label).lower() for w in ("menu", "order", "food"))
        if host and wanted and not any(h in host for h in NON_MENU_HOSTS):
            found.append((url, label))
    return list(dict.fromkeys(found))

def menu_links_from_page(page, base_url, hosts):
    """(url, label) for each link on the page that looks like a menu or ordering link, on this site, a known ordering
    platform or a PDF."""
    found = [(u, label) for u, label in embedded_nav_links(page)
             if not any(w in (u + " " + label).lower() for w in SKIP_MENU_WORDS)]
    # Links a script builds as text ("/menu/' + tmgmenu + '") sit inside <script> blocks and point nowhere real
    page = re.sub(r"(?is)<script\b.*?</script>", "", page)
    for href, label in ANCHOR_RE.findall(page):
        text = re.sub(r"<[^>]+>", "", label).lower()
        if not (any(w in href.lower() for w in MENU_LINK_WORDS) or any(w in text for w in MENU_LINK_WORDS)):
            continue
        url = urllib.parse.urljoin(base_url, unescape(href).split("#")[0])
        if any(w in (url + " " + text).lower() for w in SKIP_MENU_WORDS):
            continue
        p = urllib.parse.urlparse(url)
        netloc = p.netloc.lower().removeprefix("www.")
        # Menu PDFs are often served from a CDN rather than the restaurant's own domain
        is_pdf = p.path.lower().endswith(".pdf")
        if p.scheme in ("http", "https") and (netloc in hosts or any(h in netloc for h in ORDERING_HOSTS) or is_pdf):
            found.append((url, text))
    return found

def prefer_local_ordering(links, where):
    """Ordering-platform links often come one per location (NWB lists Toast pages "for Lisle" and "for Plainfield").
    When one of them names this venue's city, drop the other ordering links so another location's menu is not read."""
    if not where:
        return links
    _, cities = local_markers(where)
    ordering = [l for l in links if any(h in urllib.parse.urlparse(l[0]).netloc.lower() for h in ORDERING_HOSTS)]
    local = [l for l in ordering if any(c in (l[0] + " " + l[1]).lower() for c in cities)]
    if not local:
        return links
    return [l for l in links if l not in ordering or l in local]

def location_pages(page, site, hosts, where):
    """Links on the home page to the page for this venue's own city (not another city's), from the venue's address."""
    _, cities = local_markers(where)
    pages = []
    for href, label in ANCHOR_RE.findall(page):
        text = re.sub(r"<[^>]+>", "", label).lower()
        url = urllib.parse.urljoin(site, unescape(href).split("#")[0])
        p = urllib.parse.urlparse(url)
        path = p.path.strip("/").lower()
        if p.netloc.lower().removeprefix("www.") not in hosts or not path or "/" in path:
            continue
        if any(w in path for w in LOCATION_SKIP_WORDS):
            continue
        if any(c in (path + " " + text).replace("-", " ") for c in cities):
            pages.append(url)
    return list(dict.fromkeys(pages))[:3]

IMG_TAG_RE = re.compile(r"(?is)<img\b[^>]*>")
IMG_ATTR_RE = re.compile(r"(?is)(?<![\w-])(?:data-)?(?:lazy-)?src\s*=\s*[\"']([^\"']+)[\"']")
IMG_ALT_RE = re.compile(r"(?is)(?<![\w-])alt\s*=\s*[\"']([^\"']*)[\"']")

def menu_images_from_page(page, base_url):
    """Menu pictures embedded on a page, by file name or alt text (Capri Sogno shows its whole dinner menu as one PNG
    with no link to it). Returns image URLs, best first, for Gemini to read."""
    page = re.sub(r"(?is)<script\b.*?</script>", "", page)
    found = []
    for tag in IMG_TAG_RE.findall(page):
        alt = (IMG_ALT_RE.search(tag) or [None, ""])[1]
        for src in IMG_ATTR_RE.findall(tag):
            url = urllib.parse.urljoin(base_url, unescape(src).split("#")[0])
            p = urllib.parse.urlparse(url)
            text = (urllib.parse.unquote(p.path) + " " + alt).lower()
            if (p.scheme in ("http", "https") and os.path.splitext(p.path.lower())[1] in IMAGE_TYPES
                    and any(w in text for w in ("menu", "dinner", "food"))
                    and not any(w in text for w in SKIP_MENU_WORDS)):
                found.append((url, alt.lower()))
    return [u for u, _ in sorted(dict.fromkeys(found), key=lambda c: -menu_link_score(*c))][:3]

LOCATION_HUB_WORDS = ("location", "find-us", "findus", "restaurants", "local")

def location_hubs(page, base_url, hosts):
    """Links on a page to a list of the chain's locations (Ted's: Order Online and Locations both go to /locations.html)."""
    hubs = []
    for href, label in ANCHOR_RE.findall(page):
        text = re.sub(r"<[^>]+>", "", label).lower()
        url = urllib.parse.urljoin(base_url, unescape(href).split("#")[0])
        p = urllib.parse.urlparse(url)
        path = p.path.lower()
        if p.scheme not in ("http", "https") or p.netloc.lower().removeprefix("www.") not in hosts:
            continue
        if any(w in path for w in LOCATION_SKIP_WORDS):
            continue
        if any(w in path + " " + text for w in LOCATION_HUB_WORDS):
            hubs.append(url)
    return list(dict.fromkeys(hubs))[:2]

def city_page_menu_links(home_page, home_url, hosts, where):
    """(url, label) menu links for a venue whose city page is not linked from the home page. A chain's home page often
    sends visitors to a list of locations, and only the venue's own city page links its menu (a PDF). Follows the
    locations link, picks this venue's city page from the address, and collects the menu links on it, reading the page
    in a browser when its links are drawn by script."""
    links = []
    for hub in location_hubs(home_page, home_url, hosts):
        got = get_page(hub)
        if not got:
            continue
        hub_page, hub_final = got
        hub_hosts = hosts | {_host(hub_final)}
        for city_url in location_pages(hub_page, hub_final, hub_hosts, where)[:2]:
            city = get_page(city_url)
            found = menu_links_from_page(city[0], city[1], hub_hosts | {_host(city[1])}) if city else []
            if not any(u.lower().split("?")[0].endswith(".pdf") for u, _ in found):
                rendered = render_page_html(city_url)
                if rendered:
                    found = menu_links_from_page(rendered[0], rendered[1], hub_hosts | {_host(rendered[1])}) + found
            if found:
                print(f"Found the menu links on the city page {city_url}: {[u for u, _ in found][:6]}")
            links += found
        if links:
            break
    return links

def menu_page_candidates(site, where=""):
    """Menu pages for a restaurant: links on its home page that look like menu or ordering links (same site or a
    known ordering platform), links on the page for its own city when the home page is a city picker, then common
    menu paths. A site that sends visitors to another domain (or refuses plain requests) is read through a browser."""
    parsed = urllib.parse.urlparse(site)
    found = []
    locations = []
    city_links = []
    home = get_page(site)
    if home:
        page, final = home
        hosts = {_host(site), _host(final)}
        found = menu_links_from_page(page, final, hosts)
        if where:
            for loc in location_pages(page, final, hosts, where):
                got = get_page(loc)
                if got:
                    loc_page, loc_final = got
                    found += menu_links_from_page(loc_page, loc_final, hosts | {_host(loc_final)})
                locations.append(loc.rstrip("/") + "/menu")
        if where and not locations:
            city_links = city_page_menu_links(page, final, hosts, where)
    found = [u for u, _ in sorted(dict.fromkeys(prefer_local_ordering(found, where)), key=lambda c: -menu_link_score(*c))]
    # On the venue's own city page a PDF is the menu itself; the other menu links are navigation back to the chain's pages
    city_found = [u for u, _ in sorted(dict.fromkeys(city_links),
                                       key=lambda c: -(menu_link_score(*c) + (2 if c[0].lower().split("?")[0].endswith(".pdf") else 0)))]
    found = city_found + found
    found += locations
    for path in MENU_PATHS:
        found.append(f"{parsed.scheme}://{parsed.netloc}{path}")
    return list(dict.fromkeys(found))[:14]

def pricing_from_website_menu(site, where=""):
    """Try the restaurant's own menu pages (HTML or PDF). A page with no prices (a menu hub that links to Food and
    Drinks pages) is mined for menu links, which are tried next. Returns (pricing, url) or (None, None)."""
    queue = menu_page_candidates(site, where)
    tried = set()
    while queue and len(tried) < 16:
        url = queue.pop(0)
        if url in tried:
            continue
        tried.add(url)
        if _host(url) in CHALLENGED_HOSTS:
            continue  # the site shows a bot check on every page; ordering platforms it links to are still tried
        if browser_launches >= BROWSER_LIMIT and _host(url) in BLOCKED_HOSTS:
            continue  # a plain request would be refused again and the browser's launches are spent
        html, pricing = None, None
        got = fetch_url(url, with_url=True)
        mime = menu_file_mime(url, got[1]) if got else None
        if got and mime:
            # A PDF or an image of the menu (a flyer with no prices simply returns nothing)
            pricing = clean_pricing(fetch_pdf_pricing(url, mime))
            if pricing and sum(len(v) for v in pricing.values()) < 6:
                menu_log.step("pdf", "too_few_prices", url=url, n=sum(len(v) for v in pricing.values()))
                pricing = None
        elif got and got[1] and not re.search(r"text|html|xml|json", got[1], re.I):
            print(f"Skipping {url}: {got[1]} is not a page.")
            menu_log.step("fetch", "not_a_page", url=url, detail=got[1])
            continue
        elif got:
            html = got[0].decode("utf-8", "ignore")
            text = html_to_text(html)
            pricing = pricing_from_menu_text(text, where="page_html", url=url)
            if MORE_RE.search(text):
                menu_log.step("page", "load_more", url=url)
            if not pricing or MORE_RE.search(text):
                # Too few prices in the HTML usually means JavaScript draws the menu; a "Load more" control means the
                # page lists only some of the dishes until it is clicked
                rendered = render_page_text(url)
                pricing = (pricing_from_menu_text(rendered, where="page_browser", url=url) if rendered else None) or pricing
        elif _host(url) in BLOCKED_HOSTS:
            # The site turns away plain requests; a browser may not be
            rendered = render_page_text(url)
            pricing = pricing_from_menu_text(rendered, where="page_browser", url=url) if rendered else None
        if pricing:
            menu_log.step("site_pages", "ok", url=url, n=len(tried))
            return pricing, url
        page, final = RENDERED_HTML.get(url) or ((html, url) if html else (None, None))
        if page:
            hosts = {_host(site), _host(url), _host(final)}
            links = [u for u, _ in sorted(dict.fromkeys(prefer_local_ordering(menu_links_from_page(page, final, hosts), where)), key=lambda c: -menu_link_score(*c))]
            # A page that shows its menu as a picture has no prices to read as text; the picture goes first
            links = menu_images_from_page(page, final) + links
            new = [u for u in links if u not in tried and u not in queue][:6]
            if new:
                print(f"{url} has no prices; trying the menu links on it: {new}")
                menu_log.step("page", "no_prices_links_followed", url=url, links=len(new), platform=menu_log.platform_of(final, page))
                queue = new + queue
    menu_log.step("site_pages", "bot_challenge" if _host(site) in CHALLENGED_HOSTS else "no_menu_page_found", url=site, n=len(tried))
    return None, None

def _gemini_read_site(name, site, hint=""):
    prompt = (f"Open {site} and find the dinner (or all-day dine-in) menu for \"{name}\". Follow the site's menu or "
              "ordering links if the home page has no prices. Use only prices printed on the restaurant's own menu "
              "pages, never estimates or third-party listings. When the menu has separate lunch and dinner sections, "
              "use the dinner prices only; ignore lunch specials, kids menus, party packages and catering. "
              + hint + "If you cannot open a page with real dish prices, return {\"casualMains\": []}.\n" + PDF_PROMPT)
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
        config=types.GenerateContentConfig(tools=[types.Tool(url_context=types.UrlContext())], temperature=0.1),
    )
    raw = response.text.strip().replace("```json", "").replace("```", "")
    return clean_pricing(json.loads(raw))

def _premium_looks_low(pricing):
    """A premium main at or near the casual main usually means lunch or combo prices were read."""
    prem, casual = pricing.get("premiumMains"), pricing.get("casualMains")
    return bool(prem) and statistics.median(prem) < statistics.median(casual) * 1.25

def pricing_from_gemini_reading_site(name, site):
    """Last attempt at real prices before a plain web search: have Gemini open the restaurant's own menu pages
    itself, which covers menus that need JavaScript to show. Returns pricing or None."""
    try:
        pricing = _gemini_read_site(name, site)
        if pricing and _premium_looks_low(pricing):
            print("Premium mains came out close to casual mains; asking Gemini to re-check for dinner prices.")
            retry = _gemini_read_site(name, site, "A first read gave entree prices that look like lunch prices; "
                                                   "find the dinner entrees (steak, seafood, hibachi dinners) and use those. ")
            if retry and not _premium_looks_low(retry):
                pricing = retry
            else:
                print("Premium mains still look low; keeping the first read.")
                menu_log.step("site_gemini", "lunch_prices_suspected", url=site)
        if pricing and sum(len(v) for v in pricing.values()) >= 6:
            menu_log.step("site_gemini", "ok", url=site, n=sum(len(v) for v in pricing.values()))
            return pricing
        print("Gemini could not read enough real prices from the restaurant's site.")
        menu_log.step("site_gemini", "too_few_prices", url=site, n=sum(len(v) for v in pricing.values()) if pricing else 0)
    except Exception as e:
        print(f"Gemini site read error for {site}: {e}")
        menu_log.step("site_gemini", "error", url=site, detail=e)
    return None

def _is_google_search_url(url):
    """True for a google.com results page (/search), as opposed to a Maps address."""
    parsed = urllib.parse.urlparse((url or "").strip())
    return (parsed.hostname or "").lower() in ("google.com", "www.google.com") and parsed.path.startswith("/search")

def normalize_maps_url(value):
    """Return the link if it is a Google Maps address, else "". Used for the fallback link on the popup."""
    value = (value or "").strip().split()[0] if (value or "").strip() else ""
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return ""
    is_maps = (
        host in ("maps.app.goo.gl", "maps.google.com", "g.page")
        or (host == "goo.gl" and parsed.path.startswith("/maps"))
        or (host in ("google.com", "www.google.com") and parsed.path.startswith("/maps"))
    )
    if not is_maps:
        return ""
    if host == "maps.app.goo.gl":
        return f"{parsed.scheme}://{host}{parsed.path}"  # drop tracking suffixes such as ?g_st=...
    return value

def find_maps_url_in_text(text):
    """First Google Maps address anywhere in the text. Lets an issue created by a script (for example the iPhone
    Shortcut) hold the link on the same line as the header, or with no header at all."""
    for match in re.finditer(r"https?://[^\s<>\"')\]]+", text or ""):
        url = normalize_maps_url(match.group(0).rstrip(".,;:"))
        if url:
            return url
    return ""

def name_from_title(title):
    """The issue title as a venue name, ignoring the '[New Venue]:' prefix the full form pre-fills."""
    return re.sub(r"^\s*\[new venue\]\s*:?\s*", "", title or "", flags=re.IGNORECASE).strip() or None

def derive_name_from_place_text(text):
    """'Shahirizada Restaurant, 3124 IL-59, Naperville, IL' -> 'Shahirizada Restaurant'.
    Returns None when the link only holds an address (the name would start with a house number)."""
    first = (text or "").split(",")[0].strip()
    if not first or re.match(r"^\d", first):
        return None
    return first

def name_key(name):
    """Compare restaurant names ignoring case, punctuation and curly-versus-straight quotes."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())

def clean_label(text, limit=40):
    """Make a short, plain label: one line, no markup characters, no surrounding punctuation."""
    text = re.sub(r"[\r\n]+", " ", text or "")
    text = re.sub(r"[<>\"`{}\[\]*_]", "", text)
    return text.strip(" .'-")[:limit].strip()

# Umbrella terms that say too little about what a restaurant serves
VAGUE_CUISINE_WORDS = {
    "mediterranean", "asian", "international", "ethnic", "fusion", "global", "world", "eclectic",
    "european", "latin", "middle", "eastern",
}
# Words that describe the format, not the food
CUISINE_FILLER_WORDS = {
    "modern", "casual", "upscale", "elevated", "cuisine", "restaurant", "food", "bistro", "grill",
    "cafe", "kitchen", "bar", "and",
}

def is_vague_cuisine(label):
    """True when every meaningful word in the label is an umbrella term, e.g. 'Mediterranean Bistro' or 'Asian Fusion'."""
    words = [w for w in re.findall(r"[a-z]+", (label or "").lower()) if w not in CUISINE_FILLER_WORDS]
    return not words or all(w in VAGUE_CUISINE_WORDS for w in words)

def ask_gemini_for_cuisine(name, where, examples, avoid=None):
    prompt = (
        f'What cuisine best describes the restaurant "{name}"'
        + (f' ({where})' if where else '')
        + (' in the area it is located in?' if where else ' in the Naperville, IL area?')
        + ' Check the restaurant\'s own menu or website and name the specific '
        'cuisine it serves, such as the country or regional cuisine (for example Portuguese, Szechuan, Levantine), '
        'not a broad umbrella term like Mediterranean, Asian, International or Fusion. '
        f'Existing labels on the map: {examples}. Reuse one of them only if it is an accurate fit; '
        'otherwise give a new label of one to four words. '
        + (f'"{avoid}" was too vague, so do not answer with it. ' if avoid else '')
        + 'Return only the label, with no explanation, quotes or punctuation.'
    )
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.0
        ),
    )
    lines = (response.text or "").strip().splitlines()
    return clean_label(lines[0]) if lines else None

def guess_cuisine_via_gemini(name, where, known_categories):
    """Best-guess cuisine label for a restaurant, specific rather than an umbrella term; asks again once if the first answer is vague."""
    try:
        examples = ", ".join(sorted({c for c in known_categories if c and not is_vague_cuisine(c)}))
        label = ask_gemini_for_cuisine(name, where, examples)
        if label and is_vague_cuisine(label):
            print(f"Cuisine guess '{label}' for {name} is too vague; asking for something more specific.")
            retry = ask_gemini_for_cuisine(name, where, examples, avoid=label)
            if retry and not is_vague_cuisine(retry):
                return retry
            return None
        return label
    except Exception as e:
        print(f"Gemini cuisine guess error for {name}: {e}")
        return None

# Pages that are about a restaurant but are not its own website
NOT_A_WEBSITE = (
    "facebook.", "instagram.", "yelp.", "tripadvisor.", "opentable.", "doordash.", "ubereats.", "grubhub.",
    "postmates.", "seamless.", "google.", "goo.gl", "mapquest.", "yellowpages.", "restaurantji.", "wanderlog.",
    "toasttab.", "order.online", "chownow.", "tiktok.", "twitter.", "x.com", "youtube.", "yahoo.", "bing.",
    "wikipedia.", "reddit.", "nextdoor.", "patch.com", "atly.com", "frankiapp.",
)
NAME_STOPWORDS = {"the", "and", "of", "restaurant", "naperville", "il", "illinois", "south", "north", "east", "west"}

def local_markers(where):
    """Zip codes and city names from the venue's own address text, so a site is checked against the town the
    venue is really in. Falls back to Naperville when the text has neither."""
    zips = re.findall(r"\b\d{5}\b", where or "")
    cities = [c.strip().lower() for c in re.findall(r",\s*([A-Za-z][A-Za-z .'-]*?),?\s+(?:IL|Illinois)\b", where or "")]
    if not zips and not cities:
        cities = ["naperville"]
    return zips, cities

def find_website_via_gemini(name, where):
    """Ask Gemini (with web search) for the restaurant's own website. Returns a URL or None. Always verify it."""
    try:
        prompt = (
            f'What is the official website of the restaurant "{name}"'
            + (f' ({where})' if where else '')
            + (' in the area it is located in. ' if where else ' in the Naperville, IL area. ')
            + 'If the chain has a page for this specific location, give that page. '
            'Do not give Facebook, Instagram, Yelp, TripAdvisor, OpenTable, delivery-app, map or directory pages. '
            'Return only the URL, or the single word NONE if you are not sure.'
        )
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.0
            ),
        )
        match = re.search(r"https?://[^\s<>\"')\]]+", response.text or "")
        return match.group(0).rstrip(".,;:") if match else None
    except Exception as e:
        print(f"Gemini website lookup error for {name}: {e}")
        return None

# Set when Google says the day's Places quota is used up: every further lookup would fail the same way
places_quota_spent = False
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = "places.displayName,places.formattedAddress,places.location,places.websiteUri"
# Centered on Naperville; only a bias, so a more exact match elsewhere still wins
PLACES_BIAS = {"circle": {"center": {"latitude": 41.75, "longitude": -88.15}, "radius": 30000.0}}

def clean_listing_website(url):
    """Google's website field often carries tracking parameters (?utm_source=...); keep the plain address."""
    parsed = urllib.parse.urlparse((url or "").strip())
    query = [p for p in parsed.query.split("&") if p and not p.lower().startswith(("utm_", "fbclid", "gclid"))]
    return urllib.parse.urlunparse(parsed._replace(query="&".join(query), fragment=""))

def places_lookup(name, where):
    """Look the venue up in Google's Places API (the data behind the Maps listing). Returns
    {"coords": [lon, lat], "website": str, "name": str, "address": str} for the best match, else None.
    Needs GOOGLE_PLACES_API_KEY; without it the importer falls back to the older address/search methods."""
    global places_quota_spent
    key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not key or places_quota_spent:
        return None
    query = f"{name}, {where}" if where and name.lower() not in where.lower() else (where or name)
    body = json.dumps({"textQuery": query, "maxResultCount": 5, "locationBias": PLACES_BIAS}).encode()
    req = urllib.request.Request(PLACES_URL, data=body, headers={
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": PLACES_FIELDS,
    })
    places = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                places = json.load(resp).get("places") or []
            places_usage.record(PLACES_FIELDS, 200)
            break
        except urllib.error.HTTPError as e:
            places_usage.record(PLACES_FIELDS, e.code)
            # Google explains a refusal in the body (which quota, which limit); the status line alone does not
            try:
                detail = e.read().decode("utf-8", "ignore").strip().replace("\n", " ")[:400]
            except Exception:
                detail = ""
            # A daily quota does not pass in seconds: it resets at midnight Pacific time, so retrying only wastes a minute
            if e.code == 429 and "per day" in detail:
                places_quota_spent = True
                print(f"Places API: the daily quota is used up, so no lookups will work until it resets (midnight Pacific): {detail}")
                return None
            # 429 (too many requests) and 5xx pass if the call is repeated a little later
            if e.code in (429, 500, 502, 503) and attempt < 3:
                print(f"Places API lookup got HTTP {e.code}: {detail}; trying again in {10 * 2 ** attempt} seconds.")
                time.sleep(10 * 2 ** attempt)
                continue
            print(f"Places API lookup failed: {e}: {detail}")
            return None
        except Exception as e:
            places_usage.record(PLACES_FIELDS, 0)
            print(f"Places API lookup failed: {e}")
            return None
    tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in NAME_STOPWORDS]
    zips, _ = local_markers(where)
    for p in places:
        shown = (p.get("displayName") or {}).get("text", "")
        address = p.get("formattedAddress", "")
        haystack = re.sub(r"[^a-z0-9]", "", shown.lower())
        named = bool(tokens) and sum(t in haystack for t in tokens) >= max(1, round(0.6 * len(tokens)))
        in_zip = (not zips) or any(z in address for z in zips)
        loc = p.get("location") or {}
        if named and in_zip and "latitude" in loc and "longitude" in loc:
            print(f"Places API matched '{shown}' at {address}")
            return {
                "coords": [round(loc["longitude"], 6), round(loc["latitude"], 6)],
                "website": clean_listing_website(p.get("websiteUri", "")),
                "name": shown,
                "address": address,
            }
    print(f"Places API: no result matching '{name}' ({len(places)} returned)")
    return None

def verify_website(url, name, where):
    """Accept a website only if it loads, is not a directory or delivery page, and its text shows both the
    restaurant's name and its location (city or zip from the venue's address, Naperville if none) (so a same-named business elsewhere is rejected)."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        return False
    if any(bad in parsed.netloc.lower() for bad in NOT_A_WEBSITE):
        print(f"Website rejected (directory or delivery page): {url}")
        return False
    got = get_page(url)
    if not got:
        print(f"Website could not be checked: {url}")
        return False
    text = got[0][:400000].lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in NAME_STOPWORDS]
    named = bool(tokens) and sum(t in text for t in tokens) >= max(1, round(0.6 * len(tokens)))
    zips, cities = local_markers(where)
    compact = re.sub(r"[^a-z0-9]", "", text)
    local = any(z in text for z in zips) or any(c in text or re.sub(r"[^a-z0-9]", "", c) in compact for c in cities)
    if not (named and local):
        print(f"Website rejected (name found: {named}, city/zip found: {local}): {url}")
        return False
    return True

def haversine_m(a, b):
    """Distance in meters between two [lon, lat] points."""
    rad = math.pi / 180
    d_lat, d_lon = (b[1] - a[1]) * rad, (b[0] - a[0]) * rad
    h = math.sin(d_lat / 2) ** 2 + math.cos(a[1] * rad) * math.cos(b[1] * rad) * math.sin(d_lon / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

NEARBY_VENUE_M = 100

def nearby_venues(venue_id, coords, venues, radius_m=NEARBY_VENUE_M):
    """Other saved venues within radius_m of coords, as (name, metres) pairs, closest first."""
    found = [(v["name"], haversine_m(coords, v["coords"])) for v in venues
             if v.get("id") != venue_id and v.get("coords")]
    return sorted((n, round(m)) for n, m in found if m <= radius_m)

# Highest plausible dine-in price per category. Catering trays and per-pan prices land far above these.
PRICE_CEILINGS = {"appetizer": 35, "casual main": 65, "premium main": 120, "drink": 25, "dessert": 25}

def price_sanity_warnings(app, casual, premium, drink, dessert, defaulted=(), estimated=False, casual_count=1):
    """Reasons the saved prices may be off. A venue with any of these is left open for a second look. Prices from a
    web search or defaults (estimated) get the lunch-price check; a menu read from the restaurant's own site, a PDF or
    a paste is not second-guessed for a premium main close to the casual main (pizza and bar menus top out there).
    casual is the price of one casual main. casual_count is 2 for a taco place, whose casual order is two tacos: that
    order cost is what the appetizer, drink and dessert are compared with, while the tier checks use the single price
    (one taco is not the same dish as the premium main)."""
    warnings = []
    order = casual * casual_count
    for label, value in (("appetizer", app), ("casual main", casual), ("premium main", premium), ("drink", drink), ("dessert", dessert)):
        if value > PRICE_CEILINGS[label]:
            warnings.append(f"the {label} (${value}) is unusually high for one person, which often means catering or "
                            "party-package prices were read")
    if defaulted:
        warnings.append("no menu price was found for the " + ", ".join(defaulted) + ", so a typical stand-in was saved")
    if premium < casual:
        # Never right whatever the source: the tiers were probably sorted wrongly (a seafood plate read as casual, say)
        warnings.append(f"the premium main (${premium}) is lower than the casual main (${casual}), so the dishes may have "
                        "been sorted into the wrong tiers")
    elif estimated and premium < casual * 1.25:
        warnings.append(f"the premium main (${premium}) is within 25% of the casual main (${casual}), which often means "
                        "lunch or combo prices were read")
    for label, value in (("appetizer", app), ("drink", drink), ("dessert", dessert)):
        if value > order * 1.1:
            warnings.append(f"the {label} (${value}) costs more than the casual main (${order:g})")
    return warnings

# The workflows start over from the new main when a push is rejected, which runs this script again. Gemini does not
# answer the same way twice, so the first prices are kept in this file (outside the repository) and reused.
# Where saved venues are read and written; the Add City run points this at a scratch copy so it never touches venues.json
VENUES_FILE = os.environ.get("VENUES_FILE", "venues.json")

PRICING_CACHE_FILE = os.environ.get("PRICING_CACHE_FILE", "")

def load_pricing_cache():
    """Prices and their source saved by an earlier attempt of this run, or None."""
    if not PRICING_CACHE_FILE or not os.path.exists(PRICING_CACHE_FILE):
        return None
    try:
        with open(PRICING_CACHE_FILE) as f:
            saved = json.load(f)
        return saved["pricing"], saved["source"]
    except Exception as e:
        print(f"Could not read the saved prices: {e}")
        return None

def save_pricing_cache(pricing, source):
    if not PRICING_CACHE_FILE or not pricing:
        return
    try:
        with open(PRICING_CACHE_FILE, "w") as f:
            json.dump({"pricing": pricing, "source": source}, f)
    except Exception as e:
        print(f"Could not save the prices: {e}")

def set_output(key, value, multiline=False):
    """Hand a result to the GitHub Actions workflow (used for the issue comment and title)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            if multiline:
                f.write(f"{key}<<EOF_{key}\n{value}\nEOF_{key}\n")
            else:
                one_line = re.sub(r"[\r\n]+", " ", str(value))  # computed outside the f-string: Python 3.11 forbids backslashes inside one
                f.write(f"{key}={one_line}\n")

def abort(message):
    print(f"ABORT: {message}")
    set_output("error", message)
    sys.exit(1)

def main():
    issue_body = os.environ.get("ISSUE_BODY", "")

    name = extract_field(issue_body, "Restaurant Name")
    category = extract_field(issue_body, "Cuisine Category")
    address = extract_field(issue_body, "Street Address or City")
    # The one-field "from a Maps link" form (its label was "Google Maps Link" before it became "(required)")
    maps_link = (extract_field(issue_body, "Google Maps link (required)") or extract_field(issue_body, "Google Maps Link")
                 or find_maps_url_in_text(issue_body))
    coordinates_input = extract_field(issue_body, "Coordinates or Google Maps Link (Optional)") or maps_link
    website = normalize_website(extract_field(issue_body, "Website URL"))
    menu_url = extract_field(issue_body, "Online Menu / Ordering URL")
    manual_pricing_text = extract_field(issue_body, "Manual Pricing Overrides (Optional)")
    # A reply to a "needs menu" issue arrives as MENU_TEXT_OVERRIDE (see the add-menu workflow)
    menu_override = os.environ.get("MENU_TEXT_OVERRIDE", "").strip()
    menu_text = menu_override or extract_field(issue_body, "Menu text (Optional)")

    # A Maps share link carries the place's name and address; use them for anything the form left blank
    place_text = place_text_from_maps_url(coordinates_input) or place_text_from_maps_url(address)
    if not name:
        name = derive_name_from_place_text(place_text)
        if name:
            print(f"Restaurant name taken from the Maps link: {name}")
    if not name:
        name = name_from_title(os.environ.get("ISSUE_TITLE", ""))
        if name:
            print(f"Restaurant name taken from the issue title: {name}")
    if not name:
        abort("Could not find a restaurant name. Type it in the issue title (or the Restaurant Name field), or share the "
              "restaurant's page from Google Maps (not just a dropped pin) and paste that link.")

    # The Google Maps link that was provided (if any) is kept so the popup can link to it when no website is found
    maps_url = next((u for u in (normalize_maps_url(f) for f in (maps_link, coordinates_input, address)) if u), "")

    # Extract exact coordinates: try the Coordinates field first, then the address field
    coords = extract_coords_from_google_maps_url(coordinates_input) or extract_coords_from_google_maps_url(address)
    coords_exact = bool(coords)

    # Google's Places API holds what the Maps listing shows: the exact pin and the website. Ask it when either is missing.
    listing = None
    if not coords or not website:
        listing = places_lookup(name, place_text or address)
    if listing and not coords:
        coords, coords_exact = listing["coords"], True
        print(f"Using the Maps listing's exact location: {coords}")
    if not coords:
        # Fallback: geocode text. A pasted Maps share link carries a place name/address;
        # try that first, then the street address.
        candidates = []
        for field in (coordinates_input, address):
            text = place_text_from_maps_url(field)
            if text:
                candidates.extend(dict.fromkeys([text, strip_business_name(text)]))
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
        abort(f"Could not determine coordinates from the Maps link / coordinates ('{coordinates_input}') or the address ('{address}'). "
              "Paste a Google Maps share link of the restaurant's page, or 'lat, lon' coordinates.")

    # Cuisine label: use what the form gave, else keep an existing venue's label, else let Gemini guess
    with open(VENUES_FILE, "r") as f:
        venues = json.load(f)
    existing_idx = next((i for i, v in enumerate(venues) if name_key(v["name"]) == name_key(name)), None)
    category = clean_label(category)
    category_guessed = False
    if not category:
        if existing_idx is not None and venues[existing_idx].get("category"):
            category = venues[existing_idx]["category"]
        else:
            category = guess_cuisine_via_gemini(name, place_text or address, [v.get("category", "") for v in venues])
            category_guessed = bool(category)
            if not category:
                category = "Restaurant"
                print("Could not guess a cuisine label; using 'Restaurant'. Edit venues.json to change it.")

    # Website: look one up only when none was given and none is saved; it is kept only if it passes verification
    website_found = False
    saved_site = existing_idx is not None and venues[existing_idx].get("website")
    website_source = "found automatically and checked"
    if not website and not saved_site:
        if listing and listing["website"] and normalize_website(listing["website"]):
            website, website_found, website_source = listing["website"], True, "from the Google Maps listing"
            print(f"Website taken from the Maps listing: {website}")
        else:
            guess = find_website_via_gemini(name, place_text or address)
            if guess and verify_website(guess, name, place_text or address):
                website, website_found = guess, True
                print(f"Website found and verified: {website}")

    # Pricing sources, best first: pasted menu text, the menu link (PDF or readable page), the restaurant's own
    # menu pages, then a Gemini web search (estimates)
    pricing, pricing_source = load_pricing_cache() or (None, None)
    reused_pricing = bool(pricing)
    if reused_pricing:
        print(f"Reusing the prices from the first attempt ({pricing_source}).")
    if not reused_pricing:
        # A retry after a rejected push reuses the first attempt's prices and was logged then
        if website_found:
            website_status = "maps_listing" if "Maps" in website_source else "found"
        else:
            website_status = "given" if website else ("saved" if saved_site else "none")
        menu_log.start(venue=name, website=website or saved_site or "", website_status=website_status,
                       menu_url=menu_url, reply=bool(menu_override))
    if not pricing and menu_text:
        pricing = pricing_from_menu_text(menu_text)
        pricing_source = "your pasted menu"
        if not pricing and menu_override:
            menu_log.finish("reply_unreadable", source=pricing_source)
            abort("I could not read prices from that text. Reply again with the full menu page (open it, select all, copy, "
                  "paste); it needs at least a handful of dish prices like $12.99.")
        if not pricing:
            print("Could not read prices from the pasted menu text; trying other sources.")
    if not pricing and menu_url and menu_url.lower().split("?")[0].endswith(".pdf"):
        pricing = clean_pricing(fetch_pdf_pricing(menu_url))
        pricing_source = "the menu PDF"
    elif not pricing and menu_url and _is_google_search_url(menu_url):
        # Google's own Menu panel is not readable: Google answers automated requests with a CAPTCHA
        print("The menu link is a Google search page, which cannot be read automatically; trying other sources. "
              "Copy the text from Google's Menu panel and paste it into 'Menu text (Optional)' instead.")
        menu_log.step("menu_link", "google_search_url", url=menu_url)
    elif not pricing and menu_url:
        got = fetch_url(menu_url)
        if got and "pdf" not in got[1].lower():
            link_html = got[0].decode("utf-8", "ignore")
            pricing = pricing_from_menu_text(html_to_text(link_html), where="page_html", url=menu_url)
            if not pricing:
                rendered = render_page_text(menu_url)
                pricing = pricing_from_menu_text(rendered, where="page_browser", url=menu_url) if rendered else None
            pricing_source = "the menu link"
    if not pricing and (website or saved_site):
        where_text = (listing or {}).get("address") or place_text or address or ""
        pricing, menu_page = pricing_from_website_menu(website or saved_site, where_text)
        pricing_source = f"the menu on the restaurant's website ({menu_page})"
    if not pricing and (website or saved_site):
        pricing = pricing_from_gemini_reading_site(name, website or saved_site)
        pricing_source = "Gemini reading the restaurant's own menu pages"
    if not pricing:
        pricing = fetch_pricing_via_web_search(name, place_text or address or "Naperville, IL", website or saved_site or "")
        pricing_source = web_search_menu_source or "a web search (estimates)"
    if not reused_pricing:
        save_pricing_cache(pricing, pricing_source)

    # Parse manual pricing overrides (e.g. "app: 10.00\ncasualMain: 18.00")
    overrides = {}
    if manual_pricing_text:
        for line in manual_pricing_text.strip().splitlines():
            m = re.match(r'\s*(\w+)\s*:\s*\$?\s*([\d.]+)\s*', line)
            if m:
                overrides[m.group(1)] = float(m.group(2))

    # Calculate medians with guarded fallbacks
    defaulted = []
    if pricing and pricing.get("casualMains"):
        casual_list = pricing.get("casualMains") or [19.00]
        casual_main = round(statistics.median(casual_list), 2)

        # Categories with no menu price keep a stand-in number (the map needs one) but are listed in
        # defaulted, so the issue says they were not read from a menu
        # Drinks are left out on purpose: many menus omit alcohol prices, and the guess below is close enough
        for key, label in (("premiumMains", "premium main"), ("apps", "appetizer"), ("desserts", "dessert")):
            if not pricing.get(key):
                defaulted.append(label)

        prem_list = pricing.get("premiumMains") or [round(casual_main * 1.6, 2)]
        premium_main = round(statistics.median(prem_list), 2)

        apps_list = pricing.get("apps") or [round(casual_main * 0.55, 2)]
        app = round(statistics.median(apps_list), 2)

        drinks_list = pricing.get("drinks") or [max(8.00, round(casual_main * 0.5, 2))]
        drink = round(statistics.median(drinks_list), 2)

        desserts_list = pricing.get("desserts") or [8.00]
        dessert = round(statistics.median(desserts_list), 2)
    elif existing_idx is not None and venues[existing_idx].get("casualMain") is not None:
        # The price lookup failed for a venue that is already on the map: keep its current prices
        old = venues[existing_idx]
        casual_main, premium_main = old["casualMain"], old["premiumMain"]
        app, drink, dessert = old["app"], old["drink"], old["dessert"]
        print("Price lookup failed; kept the venue's existing prices.")
    else:
        # Fallback to realistic Naperville full-service medians if search fails
        casual_main = 19.00
        premium_main = 34.00
        app = 12.00
        drink = 8.50
        dessert = 8.00

    # Apply manual pricing overrides if provided
    if overrides:
        defaulted = [lbl for lbl, key in (("premium main", "premiumMain"), ("appetizer", "app"), ("drink", "drink"), ("dessert", "dessert"))
                     if lbl in defaulted and key not in overrides]
        if "app" in overrides: app = overrides["app"]
        if "casualMain" in overrides: casual_main = overrides["casualMain"]
        if "premiumMain" in overrides: premium_main = overrides["premiumMain"]
        if "drink" in overrides: drink = overrides["drink"]
        if "dessert" in overrides: dessert = overrides["dessert"]

    # A Mexican or taco place with per-taco prices: Casual means two tacos (the map page does the same)
    is_taco = pricing_rules.is_taco_place(name, category) and pricing_rules.per_taco_prices(app, casual_main)
    casual_count = 2 if is_taco else 1
    total_cost = round((app + (casual_count * casual_main) + (1 * premium_main) + (2 * drink) + dessert) * 1.30)
    tier_color = calculate_tier_color(total_cost)

    next_id = max([v.get("id", 0) for v in venues], default=0) + 1
    town, state = reverse_geocode_town(coords)
    town_source = "coordinates"
    if not town:
        # The coordinate lookup names no town in unincorporated areas (and fails when Nominatim is busy). Fall back to the
        # town in the Google Places address: the listing already fetched, the address the city and cell searches pass in,
        # the text from a Maps link, and as a last resort one more Places lookup.
        sources = [(listing or {}).get("address", ""), address if not is_maps_link(address) else "", place_text]
        for text in sources:
            town, addr_state = town_from_address(text)
            if town:
                state = state or addr_state
                town_source = "address"
                break
        if not town and not listing:
            listing = places_lookup(name, place_text or address)
            town, addr_state = town_from_address((listing or {}).get("address", ""))
            if town:
                state = state or addr_state
                town_source = "Places lookup"
    if town:
        print(f"Town for the popup: {town}, {state} (from the {town_source})")
    else:
        print("WARNING: no town found from the coordinates or any address; the popup will have no Google Maps link until one is added.")

    # Overwrite if restaurant already exists, else append
    entry = {
        "id": venues[existing_idx].get("id", next_id) if existing_idx is not None else next_id,
        "name": name,
        "category": category,
        **({"city": town} if town else {}),
        **({"state": state} if state else {}),
        **({"website": website} if website else {}),
        **({"mapsUrl": maps_url} if maps_url else {}),
        "coords": coords,
        "app": app,
        "casualMain": casual_main,
        "premiumMain": premium_main,
        "drink": drink,
        "dessert": dessert,
        "tierColor": tier_color,
        **({"isTaco": True} if is_taco else {})
    }

    if existing_idx is not None:
        previous = venues[existing_idx]
        if not coords_exact and previous.get("coords"):
            entry["coords"] = previous["coords"]  # never replace saved coordinates with an address-level guess
            print("Kept the venue's existing coordinates (the new ones were only approximate).")
        entry["name"] = previous["name"]  # keep the name as already saved (the link may spell it slightly differently)
        venues[existing_idx] = {**previous, **entry}  # keeps saved extras such as website and isTapas
    else:
        venues.append(entry)

    with open(VENUES_FILE, "w") as f:
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

    # The issue comment: a short headline, a details list, then only the follow-ups that apply
    def sentence(text):
        return text[:1].upper() + text[1:]

    lines = [f"### ✅ {name} was added to the map", ""]
    lines.append(f"- **Cuisine:** {category}" + (" (best guess; edit venues.json if it is off)" if category_guessed else ""))
    lines.append("- **Location:** " + ("exact" if coords_exact else
                 "approximate, from the address. Send a share link of the place's page or exact coordinates to fix it"))
    if website_found:
        lines.append(f"- **Website:** {website} ({website_source})")
    elif not (website or saved_site):
        lines.append("- **Website:** none found, so the venue name links to your Google Maps link. Add one with the full form's Website URL field"
                     if maps_url else "- **Website:** none found. Add one with the full form's Website URL field")
    lines.append(f"- **Estimated total for two:** ${total_cost}")
    if is_taco:
        lines.append(f"- **Tacos:** the casual main (${casual_main}) looks like the price of one taco, so the total counts two")
    lines.append("")
    if pricing and pricing.get("casualMains"):
        lines.append(f"**Prices used** (from {pricing_source}): appetizer ${app}, casual main ${casual_main}, "
                     f"premium main ${premium_main}, drink ${drink}, dessert ${dessert}")
        if str(pricing_source).startswith("a web search") and not reused_pricing:
            if search_by_claude:
                lines.append("")
                lines.append("**Claude-backup deployed**")
            if search_sources:
                lines.append("")
                label = ("Search results Claude saw (it opened or cited none of them, so the prices may rest on snippets)"
                         if search_by_claude and not claude_read[0] else "Pages the web search consulted")
                lines.append(f"**{label}:** " + "; ".join(f"[{t}]({u})" for t, u in search_sources))
            else:
                lines.append("")
                lines.append("**Pages the web search consulted:** none were returned, even on a second try")
    else:
        if claude_backup_result == "ran":
            lines.append(f"**Claude-backup deployed**, but it found no usable prices ({claude_found_nothing_pages} pages checked). "
                         "Gemini's search returned no sources, so its unsourced prices were not kept.")
            lines.append("")
        elif claude_backup_result == "not run":
            lines.append("**Claude-backup did not run** (no API key, or the request failed; see the job log). "
                         "Gemini's search returned no sources, so its unsourced prices were not kept.")
            lines.append("")
        lines.append("**Prices used:** none could be found, so typical defaults were saved")

    # Prices that came from a web search (or defaults) are guesses unless every figure was typed in by hand
    guessed = not (pricing and pricing.get("casualMains")) or str(pricing_source).startswith("a web search")
    needs_menu = guessed and not all(k in overrides for k in ("app", "casualMain", "premiumMain", "drink", "dessert"))
    set_output("needs_menu", "true" if needs_menu else "false")
    if claude_backup_result:
        set_output("claude_backup", f"{claude_backup_result}:{claude_found_nothing_pages}")  # read by add_city.py for its Skipped lines
    set_output("prices_found", "true" if pricing and pricing.get("casualMains") else "false")  # false: only the typical defaults were saved
    # The menu log: what was read, from where, and what the importer had to invent. See menu_log.py.
    found_prices = bool(pricing and pricing.get("casualMains"))
    from_search = str(pricing_source).startswith("a web search")
    menu_log.finish(("web_search_only" if from_search else ("menu_read_partial" if defaulted else "menu_read")) if found_prices else "none",
                    source=pricing_source, defaulted=defaulted, pricing=pricing if found_prices else None,
                    venue_id=entry["id"], category=category, needs_menu=needs_menu,
                    final={"app": app, "casualMain": casual_main, "premiumMain": premium_main, "drink": drink, "dessert": dessert},
                    claude_backup=claude_backup_result, claude_pages=claude_found_nothing_pages if claude_backup_result else None)
    warnings = price_sanity_warnings(app, casual_main, premium_main, drink, dessert, defaulted, estimated=guessed,
                                     casual_count=casual_count)
    if str(pricing_source).startswith("a web search") and not reused_pricing and search_ungrounded:
        warnings.append("the web search returned no source pages, so these prices may come from Gemini's memory rather than from a menu")
    set_output("needs_check", "true" if warnings else "false")
    # Why the venue was flagged, one reason per line. add_city.price_and_keep saves these on the venue so a held
    # venue's review issue (venue_review.py) can say why it needed a second look.
    flag_reasons = ([sentence(w) for w in warnings]
                    + (["No menu prices were read, so these prices are estimates"] if needs_menu else []))
    if flag_reasons:
        set_output("flags", "\n".join(flag_reasons), multiline=True)
    final_coords = (venues[existing_idx] if existing_idx is not None else entry)["coords"]
    neighbors = nearby_venues(venues[existing_idx]["id"] if existing_idx is not None else entry["id"], final_coords, venues)

    if needs_menu:
        lines += ["", "### 👉 Action needed: reply with the menu", "",
                  "These prices are estimates. Open the venue's menu page, select all, copy, and paste it as a reply here. "
                  "I will update the prices and close this issue."]
    if warnings:
        lines += ["", "### ⚠️ Double-check these prices", ""] + [f"- {sentence(w)}" for w in warnings]
        if not menu_override:
            lines += ["", "If they are wrong, reply with the menu text (open the venue's menu page, select all, copy, paste) "
                          "and I will update them. If they are fine, close this issue."]
    # An address-level pin is a guess; so is any pin placed while Google's Places API could not be asked (daily quota spent)
    pin_guessed = not coords_exact and not (existing_idx is not None and venues[existing_idx].get("coords"))
    if neighbors or pin_guessed:
        lines += ["", "### 📍 Check the pin", ""]
        if pin_guessed:
            why = ("Google's Places API ran out of lookups for today, so the pin was placed from the address instead of the Maps listing. "
                   if places_quota_spent else "No exact Maps location was found, so the pin was placed from the address. ")
            lines.append(why + "Open the map and confirm it sits on the restaurant, or send a share link of the place's page to fix it.")
        if neighbors:
            listed = ", ".join(f"{n} ({m} m)" for n, m in neighbors)
            lines.append(f"{len(neighbors)} saved venue(s) sit within {NEARBY_VENUE_M} m, which may share a building or strip mall: {listed}. "
                         "If they do, confirm the pin lines up with theirs.")
    # The popup's Google Maps link is the town line, so a venue saved without a town has no link on its card
    town_missing = not (venues[existing_idx] if existing_idx is not None else entry).get("city")
    if town_missing:
        lines += ["", "### 🏙️ Town missing", "",
                  "No town could be found from the coordinates or the address, so this venue's card has no Google Maps link. "
                  "Add `city` and `state` (for example `Wheaton` and `IL`) to this venue in venues.json."]
    if not (needs_menu or warnings or neighbors or pin_guessed or town_missing):
        lines += ["", "Nothing else is needed. Closing this issue."]
    set_output("venue_name", name)
    set_output("summary", "\n".join(lines), multiline=True)

if __name__ == "__main__":
    main()
