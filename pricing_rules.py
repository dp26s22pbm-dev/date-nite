"""Pricing rules shared by the importer (add_venue_from_issue.py), the city and cell searches (add_city.py) and, in
spirit, the map page (index.html, tacoAtATime). Keep PER_TACO_MAX and PER_TACO_APP_RULE_MAX in step with the same numbers in index.html."""
import re

# A Mexican or taco place whose casual main is at or under this price is selling tacos one at a time, so the map counts
# two of them as the Casual order. Taco places that list the appetizer above the casual main are covered as well, but
# only up to PER_TACO_APP_RULE_MAX: above that the casual main is a plate of several tacos (Oh-Mar Fresh Seafood sells 3
# tacos for $16.78 to $20.78 and prices its appetizer higher), so doubling it overstates the total.
PER_TACO_MAX = 7.0
PER_TACO_APP_RULE_MAX = 10.0
_TACO_PLACE = re.compile(r"taco|taquer|mexican", re.I)

# A menu whose steak and seafood tier tops out below this is a counter-service menu (burgers, bowls, wraps), not a date spot.
COUNTER_MENU_PREMIUM_MAX = 14.0

# Stand-in drink and dessert for a per-taco menu (casual main at or under PER_TACO_MAX) that prints neither. A full-service
# $8 drink and $8 dessert cost more than the taco order itself and roughly doubled the total for Gorditas Liselena (issue 633).
CHEAP_STAND_IN_DRINK = 3.50
CHEAP_STAND_IN_DESSERT = 4.50

def stand_in_prices(casual):
    """Numbers saved for a category the menu did not price, from the price of one casual main: premium main, appetizer,
    drink, dessert. The one place these are set; the importer, the menu-reply review and menu_prices.py all use it."""
    cheap = casual <= PER_TACO_MAX
    return {
        "premiumMain": round(casual * 1.6, 2),
        "app": round(casual * 0.55, 2),
        "drink": CHEAP_STAND_IN_DRINK if cheap else max(8.00, round(casual * 0.5, 2)),
        "dessert": CHEAP_STAND_IN_DESSERT if cheap else 8.00,
    }

def is_taco_place(name, category):
    return bool(_TACO_PLACE.search(f"{name} {category}"))

def per_taco_prices(app, casual):
    return casual <= PER_TACO_MAX or (app > casual and casual <= PER_TACO_APP_RULE_MAX)

def taco_at_a_time(venue):
    """True when the venue's Casual price is for one taco, so two tacos make the order (mirrors tacoAtATime in index.html)."""
    return bool(venue.get("isTaco")) and per_taco_prices(venue.get("app") or 0, venue.get("casualMain") or 0)

def casual_count(venue):
    """How many casual mains the standard order for two holds."""
    return 2 if taco_at_a_time(venue) else 1

def looks_like_counter_menu(venue):
    """True when the saved prices look like a counter-service menu: even the premium tier is cheap. A taco place is left
    alone, since its prices are per taco."""
    return not taco_at_a_time(venue) and venue["premiumMain"] < COUNTER_MENU_PREMIUM_MAX

def menu_prices_implausibly_low(pricing, name="", category=""):
    """True when menu prices read from a restaurant's own site are so low that even the premium tier would trip the
    counter-service check (looks_like_counter_menu). That usually means the page is unedited website-template filler
    (Gray's Mill Estate's Wix page lists a $1.50 cocktail and an $8 steak) or a kids/lunch section was read, so the
    importer tries its other sources before settling for it. Taco places are exempt: their prices are per taco.
    pricing is the importer's category lists (apps, casualMains, premiumMains, drinks, desserts)."""
    casual = sorted(pricing.get("casualMains") or [])
    if not casual or is_taco_place(name, category):
        return False
    prem = sorted(pricing.get("premiumMains") or [])
    # The importer fills a missing premium tier with 1.6 times the casual main; judge the number it would save
    premium = _median(prem) if prem else _median(casual) * 1.6
    return premium < COUNTER_MENU_PREMIUM_MAX

def apps_look_misfiled(pricing):
    """True when the appetizers read from a menu cost more than its casual mains (medians, with the same 10% leeway
    as the importer's "costs more than the casual main" warning). Starters do not outprice the main course, so the
    list is almost always entrees filed in the wrong category: a shrimp cocktail, salad and soup at $20 each on a menu
    whose mains run $17 (Tequila Azteca, issue 615). Menus that sell tacos one at a time are left alone, because a
    shared appetizer does cost more than one taco there (the same cutoff as per_taco_prices)."""
    apps = sorted(pricing.get("apps") or [])
    casual = sorted(pricing.get("casualMains") or [])
    if not apps or not casual:
        return False
    return _median(casual) > PER_TACO_APP_RULE_MAX and _median(apps) > _median(casual) * 1.1

def _median(values):
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
