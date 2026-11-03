---
description: Read an uploaded menu (image, PDF, pasted text) and return the 5 Date Nite prices using the importer's logic
argument-hint: [restaurant name] [category, optional] (attach or paste the menu)
---

The user has attached or pasted a restaurant menu. Arguments: $ARGUMENTS (restaurant name and optional category; used only for the taco rule).

Produce the five prices the importer (`add_venue_from_issue.py`) would save: **app, casualMain, premiumMain, drink, dessert**. Do not edit `venues.json` or open an issue unless asked.

## Steps

1. **Get the menu.** Use the attached image, PDF or pasted text. If it is a file path, read it (PDFs and images with Read). If nothing is attached, ask for it and stop. For pasted text, save it to the scratchpad so it can be passed to `--text`.

2. **Extract prices as the importer's Gemini step does.** Apply `PDF_PROMPT` and `SCOPE_RULES` from `add_venue_from_issue.py` (lines ~24-72) exactly; re-read them if unsure. In short:
   - Dinner or all-day dine-in prices for one person. Ignore lunch, brunch, happy hour, kids, party packages, catering (trays, pans, "serves N").
   - A price can be a bare number after the dish name; calorie counts are never prices ("12.991,220 cal" is $12.99).
   - `apps`: starters, sides, soups, salads. `casualMains`: pastas, pizzas, handhelds, burgers, chicken entrees, basic sandwiches.
   - `premiumMains`: standard single-plate steaks (ribeye, strip, filet), veal chops, prime seafood (salmon, lobster, sea bass). Exclude combo or multi-protein plates, chicken and vegetarian dishes, shared or large-format cuts, wagyu, market-price items, sharing platters. If there are no premium cuts, use the top 25% highest-priced entrees.
   - `drinks`: specialty cocktails; if none are priced, house wine or draft beer. Never soft drinks or other non-alcoholic drinks.
   - `desserts`: tiramisu, cannoli, cakes, gelato.
   - If a category has no printed prices, leave its list empty. Never invent a price.

3. **Run the importer's math** with the extraction as JSON on stdin (the script imports the importer's own `clean_pricing`, sanity checks, taco rule and tier color; it needs no API keys):

   ```bash
   python3 menu_prices.py --name "NAME" --category "CATEGORY" [--text MENU_TEXT_FILE] <<'EOF'
   {"apps": [], "casualMains": [], "premiumMains": [], "drinks": [], "desserts": []}
   EOF
   ```

4. **Report.** Lead with the script's `override` value in a plain fenced code block, exactly as printed (five lines: `app`, `casualMain`, `premiumMain`, `drink`, `dessert`), ready to paste into the issue's "Manual Pricing Overrides" field. Add one line noting that any stand-in categories (see `defaulted`, plus `drink` when no drink was priced) are not menu prices. Then give total cost, tier color, and whether the taco rule applied. Under that, list any `defaulted` categories (stand-in values, not from the menu) and every `warning`, in plain terms. Show which dishes and prices went into each category so the user can check the tiering. If a figure is uncertain (blurry image, ambiguous tier, partial menu), say so rather than guessing.
