"""Turn a menu reading into the five Date Nite prices using the importer's own rules (add_venue_from_issue.py).

Used by the /menu-prices slash command. The reading (what Gemini would return in the importer) is JSON on stdin:
  {"apps": [...], "casualMains": [...], "premiumMains": [...], "drinks": [...], "desserts": [...]}

  python menu_prices.py [--name NAME] [--category CATEGORY] [--text MENU_TEXT_FILE] < reading.json

The importer's helpers are imported, not copied, so the cleaning, sanity checks, taco rule and tier color stay in step with it.
The Gemini client is stubbed because nothing here calls Gemini.
"""
import argparse
import json
import statistics
import sys
import types

# add_venue_from_issue creates a Gemini client at import time; this script has no key and does not need one
for _mod in ("google", "google.genai", "google.genai.types"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
_genai = sys.modules["google.genai"]
_genai.Client = lambda **kw: None
_genai.types = sys.modules["google.genai.types"]
sys.modules["google"].genai = _genai

import add_venue_from_issue as importer  # noqa: E402
import pricing_rules  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--text", help="file holding the menu text, to run the importer's price-count check")
    args = ap.parse_args()

    if args.text:
        with open(args.text, encoding="utf-8", errors="replace") as f:
            n = importer.price_count(f.read())
        print(f"Price-like numbers in the menu text: {n}" + ("  (the importer needs at least 6)" if n < 6 else ""))

    pricing = importer.clean_pricing(json.load(sys.stdin))
    if not pricing:
        print("No casual main priced. The importer would reject this reading and fall back to a web search or defaults.")
        sys.exit(1)

    # Same medians and stand-ins as main() in add_venue_from_issue.py
    median = statistics.median
    casual = round(median(pricing["casualMains"]), 2)
    defaulted = [label for key, label in (("premiumMains", "premium main"), ("apps", "appetizer"), ("desserts", "dessert"))
                 if not pricing.get(key)]
    premium = round(median(pricing["premiumMains"] or [round(casual * 1.6, 2)]), 2)
    app = round(median(pricing["apps"] or [round(casual * 0.55, 2)]), 2)
    drink = round(median(pricing["drinks"] or [max(8.00, round(casual * 0.5, 2))]), 2)
    dessert = round(median(pricing["desserts"] or [8.00]), 2)

    is_taco = pricing_rules.is_taco_place(args.name, args.category) and pricing_rules.per_taco_prices(app, casual)
    total = round((app + (2 if is_taco else 1) * casual + premium + 2 * drink + dessert) * 1.30)
    warnings = importer.price_sanity_warnings(app, casual, premium, drink, dessert, defaulted=defaulted)
    if pricing_rules.looks_like_counter_menu({"isTaco": is_taco, "app": app, "casualMain": casual, "premiumMain": premium}):
        warnings.append(f"the premium main (${premium}) is under ${pricing_rules.COUNTER_MENU_PREMIUM_MAX:g}, "
                        "which looks like a counter-service menu rather than a date spot")

    # The block to paste into the issue's "Manual Pricing Overrides" field (parsed by main() in the importer)
    override = "\n".join(f"{k}: {v:.2f}" for k, v in (("app", app), ("casualMain", casual), ("premiumMain", premium),
                                                      ("drink", drink), ("dessert", dessert)))
    print(json.dumps({
        "override": override,
        "app": app, "casualMain": casual, "premiumMain": premium, "drink": drink, "dessert": dessert,
        "totalCost": total, "tierColor": importer.calculate_tier_color(total),
        "tacoAtATime": is_taco, "defaulted": defaulted, "warnings": warnings,
        "counts": {k: len(v) for k, v in pricing.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
