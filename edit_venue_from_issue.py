"""Edit a venue that is already on the map, from an "Edit a Venue" issue.

The issue names the venue by restaurant name, town and state (venue_match.py finds it generously). Whatever the form
fills in replaces what is saved; everything left blank is kept. Nothing is looked up or guessed:
  - Menu text:       prices are read from the pasted text. A category the text has no prices for keeps its saved value.
                     Text that cannot be read is an error, never a fallback to a web search.
  - Price overrides: `casualMain: 18.50` lines, any subset of app, casualMain, premiumMain, drink, dessert. These win
                     over the menu text.
  - Website URL, Cuisine Category: replace the saved value.
The issue is always closed with a summary of what changed. No labels, no follow-up questions.

Reads ISSUE_BODY (and ISSUE_NUMBER for the menu log); results go to GITHUB_OUTPUT: summary, venue_name, and error on failure."""
import datetime
import json
import os
import re
import statistics
import sys

import add_venue_from_issue as add
import menu_log
import pricing_rules
import venue_match
from venue_review import PRICE_KEYS

VENUES_FILE = "venues.json"
LABELS = {"app": "appetizer", "casualMain": "casual main", "premiumMain": "premium main", "drink": "drink", "dessert": "dessert"}
# Names a person might type for each price, reduced to lowercase letters
PRICE_ALIASES = {"app": "app", "apps": "app", "appetizer": "app", "appetizers": "app", "starter": "app",
                 "casualmain": "casualMain", "casual": "casualMain", "casualmains": "casualMain",
                 "premiummain": "premiumMain", "premium": "premiumMain", "premiummains": "premiumMain",
                 "drink": "drink", "drinks": "drink", "cocktail": "drink",
                 "dessert": "dessert", "desserts": "dessert"}
POOL = {"casualMain": "casualMains", "premiumMain": "premiumMains", "app": "apps", "drink": "drinks", "dessert": "desserts"}
LINE = re.compile(r"^\s*([A-Za-z ]+?)\s*[:=]\s*\$?\s*(\d+(?:\.\d+)?)\s*$")


def fail(message):
    add.set_output("error", message)
    print(f"ABORT: {message}", file=sys.stderr)
    sys.exit(1)


def parse_overrides(text):
    """{price key: value} from `casualMain: 18.50` style lines. A line that is not one is an error, so a typo never
    gets silently dropped."""
    found = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = LINE.match(line)
        key = PRICE_ALIASES.get(re.sub(r"[^a-z]", "", m.group(1).lower())) if m else None
        if not key:
            fail(f"I could not read the price line '{line.strip()}'. Use one price per line, like `casualMain: 18.50`. "
                 f"The names are {', '.join(PRICE_KEYS)}.")
        found[key] = float(m.group(2))
    return found


def read_menu(menu_text, venue):
    """Median per category from pasted menu text, for the categories it has prices for."""
    menu_log.start(venue=venue["name"], website=venue.get("website", ""), website_status="saved", menu_url="", reply=True)
    pricing = add.pricing_from_menu_text(menu_text, require_main=False)
    if not pricing:
        menu_log.finish("reply_unreadable", source="your pasted menu")
        fail("I could not read prices from that menu text. Paste the full menu page (open it, select all, copy, paste); "
             "it needs at least a handful of dish prices like $12.99. Or fill in Price Overrides with lines like `casualMain: 18.50`.")
    found = {key: round(statistics.median(pricing[pool]), 2) for key, pool in POOL.items() if pricing.get(pool)}
    return pricing, found


def money(x):
    return f"${x:.2f}"


def and_list(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def main():
    body = os.environ.get("ISSUE_BODY", "")
    name, town, state = (add.extract_field(body, h) for h in ("Restaurant Name", "Town", "State"))
    menu_text = add.extract_field(body, "Menu text (Optional)")
    overrides = parse_overrides(add.extract_field(body, "Price Overrides (Optional)"))
    website = add.normalize_website(add.extract_field(body, "Website URL (Optional)"))
    category = add.clean_label(add.extract_field(body, "Cuisine Category (Optional)"))

    with open(VENUES_FILE) as f:
        venues = json.load(f)
    try:
        venue = venue_match.find_venue(venues, name, town, state)
    except venue_match.MatchError as e:
        fail(str(e))
    if not (menu_text or overrides or website or category):
        fail(f"Found {venue['name']} in {venue['city']}, {venue['state']}, but the form has nothing to change. "
             "Fill in the menu text, price overrides, website or cuisine.")

    old = {k: venue[k] for k in PRICE_KEYS}
    new = dict(old)
    sources, notes = [], []
    if menu_text:
        pricing, from_menu = read_menu(menu_text, venue)
        new.update(from_menu)
        sources.append("your menu")
        kept = [LABELS[k] for k in PRICE_KEYS if k not in from_menu]
        if kept:
            notes.append(f"Your menu had no {and_list(kept)} prices, so the saved {'value was' if len(kept) == 1 else 'values were'} kept.")
    new.update(overrides)
    if overrides:
        sources.append("the prices you typed")

    changes = [f"- **{LABELS[k]}:** {money(old[k])} → {money(new[k])}" for k in PRICE_KEYS if new[k] != old[k]]
    if new != old:
        # Same total as the importer: taco places count two casual mains
        is_taco = pricing_rules.is_taco_place(venue["name"], category or venue.get("category", "")) \
            and pricing_rules.per_taco_prices(new["app"], new["casualMain"])
        total = round((new["app"] + (2 if is_taco else 1) * new["casualMain"] + new["premiumMain"]
                       + 2 * new["drink"] + new["dessert"]) * 1.30)
        venue.update(new)
        venue["tierColor"] = add.calculate_tier_color(total)
        if is_taco:
            venue["isTaco"] = True
        else:
            venue.pop("isTaco", None)
        venue["lastUpdated"] = datetime.date.today().isoformat()
        # The weekly refresh leaves venues with these sources alone (see batch_update_venues.protection_reason)
        venue["priceSource"] = "hand" if overrides else "menu"
    else:
        total = None
    if website and website != venue.get("website"):
        changes.append(f"- **website:** {venue.get('website') or 'none'} → {website}")
        venue["website"] = website
    if category and category != venue.get("category"):
        changes.append(f"- **cuisine:** {venue.get('category')} → {category}")
        venue["category"] = category

    if menu_text:
        menu_log.finish("menu_read", source="your pasted menu", defaulted=[], pricing=pricing, venue_id=venue["id"],
                        category=venue.get("category"), needs_menu=False, final=new)

    with open(VENUES_FILE, "w") as f:
        json.dump(venues, f, indent=2)

    lines = [f"### ✅ {venue['name']} ({venue['city']}, {venue['state']}) was updated" if changes
             else f"### ✅ {venue['name']} ({venue['city']}, {venue['state']}) already had these values; nothing changed"]
    if venue_match.name_score(name, venue["name"], venue["city"]) < 1.0:
        lines += ["", f"Matched \"{name}\" to **{venue['name']}**."]
    if changes:
        lines += [""] + changes
    if sources and new != old:
        lines.append(f"\nPrices came from {' and '.join(sources)}." + "".join(f" {n}" for n in notes))
    if total is not None:
        lines.append(f"\n**Estimated total for two:** ${total}")
    if new["premiumMain"] < new["casualMain"]:
        lines.append(f"\nNote: the premium main ({money(new['premiumMain'])}) is lower than the casual main ({money(new['casualMain'])}). "
                     "Open a new Edit a Venue issue with price overrides if that is not what you meant.")
    summary = "\n".join(lines)
    add.set_output("venue_name", venue["name"])
    add.set_output("summary", summary, multiline=True)
    print(summary)


if __name__ == "__main__":
    main()
