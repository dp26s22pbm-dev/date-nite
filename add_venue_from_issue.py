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
        return "#22c55e"
    elif main_price < 23.00:
        return "#eab308"
    elif main_price < 33.00:
        return "#f97316"
    elif main_price < 50.00:
        return "#ef4444"
    else:
        return "#a855f7"

async def scrape_site_or_pdf(url):
    """Navigates site, finds text or embedded PDF menus, and extracts raw data."""
    pdf_bytes = None
    extracted_text = ""

    # Check if direct link is a PDF
    if url.lower().endswith(".pdf"):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            return None, res.read()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(2)

            # Look for direct menu links or PDF links on the page
            pdf_link = await page.locator('a[href$=".pdf"], a:has-text("Dinner Menu"), a:has-text("Full Menu")').first.get_attribute("href")
            if pdf_link:
                full_pdf_url = urllib.parse.urljoin(url, pdf_link)
                if full_pdf_url.lower().endswith(".pdf"):
                    print(f"Found embedded PDF menu: {full_pdf_url}")
                    req = urllib.request.Request(full_pdf_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as res:
                        pdf_bytes = res.read()
                        await browser.close()
                        return None, pdf_bytes

            # Otherwise extract DOM text
            for _ in range(3):
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.4)

            extracted_text = await page.inner_text("body")
        except Exception as e:
            print(f"Playwright navigation warning: {e}")
        finally:
            await browser.close()

    return extracted_text, None

def parse_pricing_with_gemini(text, pdf_data):
    try:
        if pdf_data:
            response = model.generate_content([
                PROMPT,
                {"mime_type": "application/pdf", "data": pdf_data}
            ])
        else:
            response = model.generate_content(f"{PROMPT}\n\nMENU TEXT:\n{text[:25000]}")

        clean = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean)
    except Exception as e:
        print(f"Gemini pricing extraction error: {e}")
        return None

async def main():
    issue_body = os.environ.get("ISSUE_BODY", "")
    
    name = extract_field(issue_body, "Restaurant Name")
    category = extract_field(issue_body, "Cuisine Category")
    address = extract_field(issue_body, "Street Address or City")
    menu_url = extract_field(issue_body, "Online Menu / Ordering URL")

    # Geocode Address
    coords = geocode_address(f"{name}, {address}") if address else None
    if not coords and address:
        coords = geocode_address(address)
    if not coords:
        coords = [-88.150000, 41.770000]

    # Scrape dynamic site or native PDF
    text, pdf_bytes = await scrape_site_or_pdf(menu_url) if menu_url else ("", None)
    pricing = parse_pricing_with_gemini(text, pdf_bytes) if (text or pdf_bytes) else None

    # Calculate medians from real data
    if pricing and pricing.get("casualMains"):
        casual_main = round(statistics.median(pricing["casualMains"]), 2)
        premium_main = round(statistics.median(pricing.get("premiumMains", [casual_main * 1.5])), 2)
        app = round(statistics.median(pricing.get("apps", [casual_main * 0.55])), 2)
        drink = round(statistics.median(pricing.get("drinks", [7.0])), 2)
        dessert = round(statistics.median(pricing.get("desserts", [casual_main * 0.4])), 2)
    else:
        # Fallback to realistic Naperville full-service medians if completely blocked
        casual_main = 19.00
        premium_main = 34.00
        app = 12.00
        drink = 8.50
        dessert = 8.00

    tier_color = calculate_tier_color(casual_main)

    with open("venues.json", "r") as f:
        venues = json.load(f)

    next_id = max([v.get("id", 0) for v in venues], default=0) + 1

    # Overwrite if restaurant already exists, else append
    existing_idx = next((i for i, v in enumerate(venues) if v["name"].lower() == name.lower()), None)
    entry = {
        "id": existing_idx + 1 if existing_idx is not None else next_id,
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

    print(f"Added {name} (ID: {entry['id']}): Casual ${casual_main}, Prime ${premium_main}, Color: {tier_color}")

if __name__ == "__main__":
    asyncio.run(main())
