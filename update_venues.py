"""Legacy batch updater — kept for parity with batch_update_venues.py.

Now uses Gemini Google Search Grounding instead of Playwright scraping.
For the primary refresh workflow, use batch_update_venues.py instead.
"""
import os
import json
import statistics
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

try:
    from urls import TARGET_URLS
except Exception as e:
    print(f"Failed to import TARGET_URLS from urls.py: {e}")
    TARGET_URLS = {}

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

def fetch_pricing_via_gemini_search(venue_name, city="Naperville, IL"):
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

def main():
    with open("venues.json", "r") as f:
        venues = json.load(f)

    for venue in venues:
        vid = venue["id"]
        if vid not in TARGET_URLS:
            continue

        print(f"Updating: {venue['name']}...")
        data = fetch_pricing_via_gemini_search(venue["name"])
        if not data:
            print(f"  Could not retrieve prices for {venue['name']}; skipping.")
            continue

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

        print(f"  Updated {venue['name']}")

    with open("venues.json", "w") as f:
        json.dump(venues, f, indent=2)

if __name__ == "__main__":
    main()
