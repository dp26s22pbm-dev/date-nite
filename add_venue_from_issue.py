import os
import re
import json
import statistics
import asyncio
import urllib.request
import urllib.parse
from playwright.async_api import async_playwright
from google import genai

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

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

def geocode_address(name, address):
    # Try querying the full address first (most accurate for exact pin placement)
    queries = []
    if address:
        if "naperville" not in address.lower() and "il" not in address.lower():
            queries.append(f"{address}, Naperville, IL")
        queries.append(address)
        queries.append(f"{name}, {address}")
    queries.append(f"{name}, Naperville, IL")

    for q in queries:
        try:
            url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(q)}&format=json&limit=1&countrycodes=us"
            req = urllib.request.Request(url, headers={"User-Agent": "NapervilleMenuMapBot/1.0 (contact@example.com)"})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                if data and len(data) > 0:
                    lon = round(float(data[0]["lon"]), 6)
                    lat = round(float(data[0]["lat"]), 6)
                    # Sanity check: must be in Illinois / Chicago western suburbs bounding box
                    if -88.5 <= lon <= -87.5 and 41.5 <= lat <= 42.1:
                        return [lon, lat]
        except Exception as e:
            print(f"Geocoding attempt failed for '{q}': {e}")
            continue

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
    if url.lower().endswith(".pdf"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as res:
                return None, res.read()
        except Exception as e:
            print(f"Error fetching PDF {url}: {e}")
            return "", None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(2)

            # Probe for PDF link without blocking execution
            try:
                pdf_elem = page.locator('a[href$=".pdf"]').first
                if await pdf_elem.count() > 0:
                    pdf_link = await pdf_elem.get_attribute("href", timeout=2000)
                    if pdf_link:
                        full_pdf_url = urllib.parse.urljoin(url, pdf_link)
                        req = urllib.request.Request(full_pdf_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=15) as res:
                            pdf_data = res.read()
                            await browser.close()
                            return None, pdf_data
            except Exception:
                pass

            # Scroll to trigger dynamic elements
            for _ in range(3):
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.4)

            text = await page.inner_text("body")
            await browser.close()
            return text, None
        except Exception as e:
            print(f"Playwright navigation warning: {e}")
            await browser.close()
            return "", None

def parse_pricing_with_gemini(text, pdf_data):
    try:
        if pdf_data:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=[
                    PROMPT,
                    genai.types.Part.from_bytes(
                        data=pdf_data,
                        mime_type="application/pdf"
                    )
                ]
            )
        else:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=f"{PROMPT}\n\nMENU TEXT:\n{text[:25000]}"
            )

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
    asyncio.run(main())
