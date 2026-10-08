import os
import re
import json
import statistics
import asyncio
from playwright.async_api import async_playwright
import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash")

PROMPT = """
You are a menu parsing engine. Return strictly valid JSON containing float price arrays:
{
  "apps": [float, ...],
  "casualMains": [float, ...],
  "premiumMains": [float, ...],
  "drinks": [float, ...],
  "desserts": [float, ...]
}
Rules:
- "apps": Appetizers/starters/small plates.
- "casualMains": Handhelds, burgers, entree salads, and pastas (typically under $30).
- "premiumMains": Center-of-plate steaks, chops, prime seafood, or signature cuts (typically $35+). If a venue doesn't have luxury cuts, copy casual entrees here.
- "drinks": Cocktails, beers, and wines by the glass.
- "desserts": Standard desserts.
Output pure JSON, no markdown formatting.
"""

def extract_field(body, header):
    pattern = rf"### {header}\s*\n\s*(.*?)(?=\n###|\Z)"
    match = re.search(pattern, body, re.DOTALL)
    return match.group(1).strip() if match else ""

async def fetch_text(page, url):
    try:
        await page.goto(url, wait_until="networkidle", timeout=35000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await asyncio.sleep(0.5)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return await page.inner_text("body")
    except Exception as e:
        print(f"Fetch failed for {url}: {e}")
        return ""

async def main():
    issue_body = os.environ.get("ISSUE_BODY", "")
    
    name = extract_field(issue_body, "Restaurant Name")
    category = extract_field(issue_body, "Cuisine Category")
    coords_raw = extract_field(issue_body, "Coordinates (Latitude, Longitude)")
    tier_raw = extract_field(issue_body, "Map Glow / Pin Tier Color")
    menu_url = extract_field(issue_body, "Online Menu / Ordering URL")

    # Parse coordinates: MapLibre uses [lng, lat]
    lat_match = re.findall(r"[-+]?\d*\.\d+|\d+", coords_raw)
    if len(lat_match) >= 2:
        lat, lng = float(lat_match[0]), float(lat_match[1])
        coords = [lng, lat]
    else:
        coords = [-88.1500, 41.7700]

    # Parse color hex code
    color_match = re.search(r"#[0-9a-fA-F]{6}", tier_raw)
    tier_color = color_match.group(0) if color_match else "#38bdf8"

    # Default fallbacks
    app = 15.00
    casual_main = 22.00
    premium_main = 38.00
    drink = 14.00
    dessert = 11.00

    # Scrape menu text & get medians
    if menu_url:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            raw_text = await fetch_text(page, menu_url)
            await browser.close()

            if raw_text:
                try:
                    res = model.generate_content(f"{PROMPT}\n\nMENU TEXT:\n{raw_text[:14000]}")
                    clean_json = res.text.strip().replace("```json", "").replace("```", "")
                    data = json.loads(clean_json)

                    if data.get("apps"):
                        app = round(statistics.median(data["apps"]), 2)
                    if data.get("casualMains"):
                        casual_main = round(statistics.median(data["casualMains"]), 2)
                    if data.get("premiumMains"):
                        premium_main = round(statistics.median(data["premiumMains"]), 2)
                    if data.get("drinks"):
                        drink = round(statistics.median(data["drinks"]), 2)
                    if data.get("desserts"):
                        dessert = round(statistics.median(data["desserts"]), 2)
                except Exception as e:
                    print(f"Parsing skipped: {e}")

    # Load and append to venues.json
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

    # Append to TARGET_URLS in update_venues.py so future updates continue to include it
    if os.path.exists("update_venues.py") and menu_url:
        with open("update_venues.py", "r") as f:
            script_content = f.read()

        target_insert = f'    {next_id}: "{menu_url}",\n}}'
        script_content = script_content.replace("}", target_insert, 1)

        with open("update_venues.py", "w") as f:
            f.write(script_content)

    print(f"Added {name} (ID: {next_id}) successfully.")

if __name__ == "__main__":
    asyncio.run(main())
