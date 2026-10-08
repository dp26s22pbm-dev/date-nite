import os
import re
import json
import statistics
import asyncio
import urllib.request
import urllib.parse
from playwright.async_api import async_playwright
import google.generativeai as genai

genai.configure(api_key=os.environ.get("GEMINI_API_KEY", ""))
model = genai.GenerativeModel("gemini-1.5-flash")

PROMPT = """
You are a restaurant menu pricing extraction engine. Analyze the raw menu text and return strictly valid JSON containing float price arrays:
{
  "apps": [float, ...],
  "casualMains": [float, ...],
  "premiumMains": [float, ...],
  "drinks": [float, ...]
  "desserts": [float, ...]
}
Rules:
- "apps": Starters, sides, appetizers, chips & dip.
- "casualMains": Handhelds, tacos, burritos, sandwiches, burgers, pizzas, pastas, salads.
- "premiumMains": Ribeyes, filets, prime seafood platters, or the highest-tier specialty combos. If there are no premium luxury cuts, duplicate the upper-half of casual mains.
- "drinks": Soft drinks, sodas, beers, wines, or cocktails.
- "desserts": Churros, cakes, sweets, or shakes.
Output pure JSON with no markdown backticks.
"""

def extract_field(body, header):
    pattern = rf"### {re.escape(header)}\s*\n\s*(.*?)(?=\n###|\Z)"
    match = re.search(pattern, body, re.DOTALL)
    return match.group(1).strip() if match else ""

def parse_manual_overrides(raw_text):
    overrides = {}
    if not raw_text:
        return overrides
    for line in raw_text.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            num_match = re.search(r"[-+]?\d*\.\d+|\d+", val)
            if num_match:
                overrides[key] = float(num_match.group(0))
    return overrides

def geocode_address(query):
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query)}&format=json&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "MenuMapBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data:
                return [round(float(data[0]["lon"]), 6), round(float(data[0]["lat"]), 6)]
    except Exception as e:
        print(f"Geocoding error: {e}")
    return None

def calculate_tier_color(main_price):
    if main_price < 16.00:
        return "#22c55e"  # Green: Casual / Value
    elif main_price < 23.00:
        return "#eab308"  # Yellow: Moderate
    elif main_price < 33.00:
        return "#f97316"  # Orange: Elevated Bistro
    elif main_price < 50.00:
        return "#ef4444"  # Red: Premium / Steakhouse
    else:
        return "#a855f7"  # Purple: High-End Luxury

async def fetch_dynamic_menu(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        try:
            await page.wait_for_selector(
                '[class*="item"], [class*="product"], [class*="menu"], [data-testid*="item"], [class*="card"]',
                timeout=12000
            )
        except Exception:
            pass

        for _ in range(4):
            await page.mouse.wheel(0, 1500)
            await asyncio.sleep(0.5)

        return await page.inner_text("body")
    except Exception as e:
        print(f"Playwright navigation failed for {url}: {e}")
        return ""

async def main():
    issue_body = os.environ.get("ISSUE_BODY", "")
    
    name = extract_field(issue_body, "Restaurant Name")
    category = extract_field(issue_body, "Cuisine Category")
    address = extract_field(issue_body, "Street Address or City")
    coords_raw = extract_field(issue_body, "Coordinates (Optional)")
    menu_url = extract_field(issue_body, "Online Menu / Ordering URL")
    manual_raw = extract_field(issue_body, "Manual Pricing Overrides (Optional)")

    # 1. Resolve Coordinates: Coordinates -> Address -> Fallback
    coords = None
    if coords_raw:
        parts = [p.strip() for p in coords_raw.replace(";", ",").split(",") if p.strip()]
        if len(parts) >= 2:
            try:
                coords = [round(float(parts[1]), 6), round(float(parts[0]), 6)]
            except ValueError:
                pass

    if not coords and address:
        print(f"Geocoding address: {address}")
        coords = geocode_address(f"{name}, {address}")
        if not coords:
            coords = geocode_address(address)

    if not coords:
        coords = [-88.150000, 41.770000]

    # 2. Check for manual user price inputs
    app = None
    casual_main = None
    premium_main = None
    drink = None
    dessert = None

    manuals = parse_manual_overrides(manual_raw)
    if manuals.get("app") is not None: app = manuals["app"]
    if manuals.get("casualMain") is not None: casual_main = manuals["casualMain"]
    if manuals.get("premiumMain") is not None: premium_main = manuals["premiumMain"]
    if manuals.get("drink") is not None: drink = manuals["drink"]
    if manuals.get("dessert") is not None: dessert = manuals["dessert"]

    # 3. Dynamic scrape if needed
    missing_fields = any(v is None for v in [app, casual_main, premium_main, drink, dessert])
    if missing_fields and menu_url:
        print(f"Scraping dynamic menu content from: {menu_url}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            raw_text = await fetch_dynamic_menu(page, menu_url)
            await browser.close()

            if raw_text and len(raw_text.strip()) > 100:
                try:
                    res = model.generate_content(f"{PROMPT}\n\nMENU TEXT:\n{raw_text[:20000]}")
                    clean_json = res.text.strip().replace("```json", "").replace("```", "")
                    data = json.loads(clean_json)

                    if app is None and data.get("apps"):
                        app = round(statistics.median(data["apps"]), 2)
                    if casual_main is None and data.get("casualMains"):
                        casual_main = round(statistics.median(data["casualMains"]), 2)
                    if premium_main is None and data.get("premiumMains"):
                        premium_main = round(statistics.median(data["premiumMains"]), 2)
                    if drink is None and data.get("drinks"):
                        drink = round(statistics.median(data["drinks"]), 2)
                    if dessert is None and data.get("desserts"):
                        dessert = round(statistics.median(data["desserts"]), 2)
                except Exception as e:
                    print(f"Gemini processing error: {e}")

    # Fallbacks if scrape fails
    if app is None: app = 8.00
    if casual_main is None: casual_main = 14.00
    if premium_main is None: premium_main = casual_main
    if drink is None: drink = 4.00
    if dessert is None: dessert = 6.00

    # 4. Automatically assign tier color based on final casualMain
    tier_color = calculate_tier_color(casual_main)

    # 5. Commit to venues.json
    with open("venues.json", "r") as f:
        venues = json.load(f)

    next_id = max([v.get("id", 0) for v in venues], default=0) + 1

    new_venue = {
        "id": next_id,
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

    venues.append(new_venue)

    with open("venues.json", "w") as f:
        json.dump(venues, f, indent=2)

    if os.path.exists("update_venues.py") and menu_url:
        with open("update_venues.py", "r") as f:
            script_content = f.read()

        target_insert = f'    {next_id}: "{menu_url}",\n}}'
        script_content = script_content.replace("}", target_insert, 1)

        with open("update_venues.py", "w") as f:
            f.write(script_content)

    print(f"Successfully processed {name} (ID: {next_id}) with color {tier_color} based on ${casual_main} casualMain")

if __name__ == "__main__":
    asyncio.run(main())
