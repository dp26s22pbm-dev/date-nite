import os
import json
import statistics
import asyncio
from playwright.async_api import async_playwright
import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash")

TARGET_URLS = {
    1: "https://order.lazydogrestaurants.com/menu/naperville",
    2: "https://haciendarealbolingbrook.toast.site/menu",
    3: "https://vaisitalianinspiredrestaurantkitchenbar.restaurants-info.com/menu",
    4: "https://www.mesonsabika.com/dinner-menu/",
    5: "https://www.stcielo.com/menu/dinner/",
    6: "https://order.toasttab.com/online/vasilis-naperville",
    7: "https://www.gordonramsayrestaurants.com/en/us/ramsays-kitchen/naperville/menus",
    8: "https://order.online/store/entourage-naperville",
    9: "https://order.online/store/hugo's-frog-bar-&-fish-house-naperville-69736",
    10: "https://tacopros.com/menu/",
    11: "traversosrestaurant.com",
}

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

async def update_all():
    with open("venues.json", "r") as f:
        venues = json.load(f)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for venue in venues:
            vid = venue["id"]
            url = TARGET_URLS.get(vid)
            if not url:
                continue

            print(f"Updating: {venue['name']}...")
            raw_text = await fetch_text(page, url)
            if not raw_text:
                continue

            try:
                res = model.generate_content(f"{PROMPT}\n\nMENU TEXT:\n{raw_text[:14000]}")
                clean_json = res.text.strip().replace("```json", "").replace("```", "")
                data = json.loads(clean_json)

                if data.get("apps"):
                    venue["app"] = round(statistics.median(data["apps"]), 2)
                if data.get("casualMains"):
                    venue["casualMain"] = round(statistics.median(data["casualMains"]), 2)
                if data.get("premiumMains"):
                    venue["premiumMain"] = round(statistics.median(data["premiumMains"]), 2)
                if data.get("drinks"):
                    venue["drink"] = round(statistics.median(data["drinks"]), 2)
                if data.get("desserts"):
                    venue["dessert"] = round(statistics.median(data["desserts"]), 2)

                print(f"✓ Updated {venue['name']}")
            except Exception as err:
                print(f"Parsing skipped for {venue['name']}: {err}")

        await browser.close()

    with open("venues.json", "w") as f:
        json.dump(venues, f, indent=2)

if __name__ == "__main__":
    asyncio.run(update_all())
