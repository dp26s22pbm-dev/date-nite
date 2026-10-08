import os
import re
import json
import statistics
import asyncio
import urllib.request
import urllib.parse
from playwright.async_api import async_playwright
# New SDK
from google import genai

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

# Import menu URL mapping
try:
    from update_venues import VENUE_URLS
except ImportError:
    VENUE_URLS = {}

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
- "casualMains": Standard pastas, pizzas, handhelds, burgers, sandwiches.
- "premiumMains": Steaks (ribeye, strip, filet), prime chops, prime seafood (salmon, lobster), or specialty combos. If there are no premium luxury cuts, use the top 25% highest priced entrees.
- "drinks": Wine, beer, cocktails, sodas.
- "desserts": Tiramisu, cannoli, cakes, gelato.
Return strictly valid JSON with no markdown formatting.
"""

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
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(2)

            pdf_link = await page.locator('a[href$=".pdf"], a:has-text("Dinner Menu"), a:has-text("Full Menu")').first.get_attribute("href")
            if pdf_link:
                full_pdf_url = urllib.parse.urljoin(url, pdf_link)
                if full_pdf_url.lower().endswith(".pdf"):
                    req = urllib.request.Request(full_pdf_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as res:
                        pdf_data = res.read()
                        await browser.close()
                        return None, pdf_data

            for _ in range(3):
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(0.4)

            text = await page.inner_text("body")
            await browser.close()
            return text, None
        except Exception as e:
            print(f"Playwright scrape error for {url}: {e}")
            await browser.close()
            return "", None

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
        print(f"Gemini error: {e}")
        return None

async def main():
    target_venue = os.environ.get("TARGET_VENUE", "").strip().lower()

    with open("venues.json", "r") as f:
        venues = json.load(f)

    updated_count = 0
    for v in venues:
        venue_id = v.get("id")
        name = v.get("name", "")

        # If TARGET_VENUE is specified (e.g. "traverso"), skip everyone else
        if target_venue and target_venue not in name.lower():
            continue

        url = VENUE_URLS.get(venue_id)
        if not url:
            print(f"Skipping {name} (ID: {venue_id}) - no menu URL registered in update_venues.py")
            continue

        print(f"Refreshing pricing for: {name} ({url})")
        text, pdf_bytes = await scrape_site_or_pdf(url)
        pricing = parse_pricing_with_gemini(text, pdf_bytes) if (text or pdf_bytes) else None

        if pricing and pricing.get("casualMains"):
            v["casualMain"] = round(statistics.median(pricing["casualMains"]), 2)
            v["premiumMain"] = round(statistics.median(pricing.get("premiumMains", [v["casualMain"] * 1.5])), 2)
            v["app"] = round(statistics.median(pricing.get("apps", [v["casualMain"] * 0.55])), 2)
            v["drink"] = round(statistics.median(pricing.get("drinks", [7.5])), 2)
            v["dessert"] = round(statistics.median(pricing.get("desserts", [v["casualMain"] * 0.4])), 2)
            v["tierColor"] = calculate_tier_color(v["casualMain"])
            print(f"Updated {name}: Casual ${v['casualMain']}, Prime ${v['premiumMain']}, Color {v['tierColor']}")
            updated_count += 1
        else:
            print(f"Could not parse new prices for {name}; keeping existing data.")

    if updated_count > 0:
        with open("venues.json", "w") as f:
            json.dump(venues, f, indent=2)
        print(f"Saved {updated_count} updated venue(s) to venues.json.")

if __name__ == "__main__":
    asyncio.run(main())
