"""Pricing rules shared by the importer (add_venue_from_issue.py), the city and cell searches (add_city.py) and, in
spirit, the map page (index.html, tacoAtATime). Keep PER_TACO_MAX in step with the same number in index.html."""
import re

# A Mexican or taco place whose casual main is at or under this price is selling tacos one at a time, so the map counts
# two of them as the Casual order. Taco places that list the appetizer above the casual main are covered as well.
PER_TACO_MAX = 7.0
_TACO_PLACE = re.compile(r"taco|taquer|mexican", re.I)

# A menu whose steak and seafood tier tops out below this is a counter-service menu (burgers, bowls, wraps), not a date spot.
COUNTER_MENU_PREMIUM_MAX = 14.0

def is_taco_place(name, category):
    return bool(_TACO_PLACE.search(f"{name} {category}"))

def per_taco_prices(app, casual):
    return casual <= PER_TACO_MAX or app > casual

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
