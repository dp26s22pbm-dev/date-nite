import os
import json
import statistics
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

try:
    from urls import TARGET_URLS as VENUE_URLS
except Exception as e:
    print(f"Failed to import TARGET_URLS from urls.py: {e}")
    VENUE_URLS = {}

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
- "casualMains": Standard pastas, pizzas, handhelds, burgers, sandwiches.
- "premiumMains": Steaks (ribeye, strip, filet), prime chops, prime seafood (salmon, lobster), or specialty combos. If there are no premium luxury cuts, use the top 25% highest priced entrees.
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

def fetch_pricing_via_gemini_search(venue_name, city="Naperville, IL"):
    """Query Gemini with Google Search Grounding to find live menu pricing."""
    try:
        prompt = SEARCH_PRICING_PROMPT.format(venue_name=venue_name, city=city)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
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

def parse_pricing_from_pdf(pdf_bytes):
    """Parse pricing from a PDF using Gemini (no search grounding needed)."""
    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
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

def apply_pricing_to_venue(v, pricing):
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
    return True

def main():
    target_venue = os.environ.get("TARGET_VENUE", "").strip().lower()

    with open("venues.json", "r") as f:
        venues = json.load(f)

    updated_count = 0
    for v in venues:
        venue_id = v.get("id")
        name = v.get("name", "")

        if target_venue and target_venue not in name.lower():
            continue

        print(f"Refreshing pricing for: {name} (ID: {venue_id})")

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

        if pdf_pricing and apply_pricing_to_venue(v, pdf_pricing):
            print(f"  Updated {name} from PDF: Casual ${v['casualMain']}, Prime ${v['premiumMain']}")
            updated_count += 1
            continue

        # Step 2: Use Gemini Google Search Grounding as the primary method
        city = "Naperville, IL"
        pricing = fetch_pricing_via_gemini_search(name, city)

        if pricing and apply_pricing_to_venue(v, pricing):
            print(f"  Updated {name} via Gemini Search: Casual ${v['casualMain']}, Prime ${v['premiumMain']}, Drinks ${v['drink']}, App ${v['app']}, Color {v['tierColor']}")
            updated_count += 1
        else:
            print(f"  Could not retrieve new prices for {name}; keeping existing data.")

    if updated_count > 0:
        with open("venues.json", "w") as f:
            json.dump(venues, f, indent=2)
        print(f"Saved {updated_count} updated venue(s) to venues.json.")

if __name__ == "__main__":
    main()
