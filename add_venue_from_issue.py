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

def extract_coords_from_google_maps_url(input_str):
    if not input_str:
        return None

    # 1. Check for raw numeric coordinates: "41.712331, -88.205216"
    raw_nums = re.findall(r'[-+]?\d+\.\d+', input_str)
    if len(raw_nums) >= 2:
        val1, val2 = float(raw_nums[0]), float(raw_nums[1])
        lat = val1 if val1 > 0 else val2
        lon = val2 if val2 < 0 else val1
        return [round(lon, 6), round(lat, 6)]

    # 2. Expand Google Maps short links or resolve share URLs
    final_url = input_str.strip()
    if "maps" in final_url or "goo.gl" in final_url:
        try:
            req = urllib.request.Request(
                final_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                final_url = resp.geturl()
        except Exception as e:
            print(f"Failed expanding map link: {e}")

    # 3. Match @lat,lon or ?q=lat,lon in expanded URL
    match = re.search(r'[@\?q=]([-+]?\d+\.\d+),([-+]?\d+\.\d+)', final_url)
    if match:
        val1, val2 = float(match.group(1)), float(match.group(2))
        lat = val1 if val1 > 0 else val2
        lon = val2 if val2 < 0 else val1
        return [round(lon, 6), round(lat, 6)]

    # 4. Match Google's protobuf data strings: !3d41.712331!4d-88.205216
    match_proto = re.search(r'!3d([-+]?\d+\.\d+)!4d([-+]?\d+\.\d+)', final_url)
    if match_proto:
        lat = float(match_proto.group(1))
        lon = float(match_proto.group(2))
        return [round(lon, 6), round(lat, 6)]

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
    coordinates_input = extract_field(issue_body, "Coordinates (Optional)")
    menu_url = extract_field(issue_body, "Online Menu / Ordering URL")
    manual_pricing_text = extract_field(issue_body, "Manual Pricing Overrides (Optional)")

    # Extract exact coordinates: try the Coordinates field first, then the address field
    coords = extract_coords_from_google_maps_url(coordinates_input) or extract_coords_from_google_maps_url(address)
    if not coords:
        print(f"ABORT: Could not parse exact coordinates from either the Coordinates field ('{coordinates_input}') or the address field ('{address}').")
        return

    # Scrape dynamic site or native PDF
    text, pdf_bytes = await scrape_site_or_pdf(menu_url) if menu_url else ("", None)
    pricing = parse_pricing_with_gemini(text, pdf_bytes) if (text or pdf_bytes) else None

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
        # Fallback to realistic Naperville full-service medians if completely blocked
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
