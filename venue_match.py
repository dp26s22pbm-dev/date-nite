"""Find a saved venue from the name, town and state typed into an "Edit a Venue" issue.

The match is generous about the name and strict about the place. Town and state narrow the search to one town's venues
first, so a short or sloppy name only has to be unique there. Differences that do not change the match: case, accents,
punctuation, "&" versus "and", a leading "The", filler words (Restaurant, Bar, Grill, Kitchen, Cafe ...), the town's own
name tacked onto the end, word order and small typos. A name that fits more than one venue in the town is refused with
the candidates listed, never guessed.

Standard library only, so it runs without the importer's API clients. Raises MatchError with a message meant to be
posted on the issue."""
import difflib
import re
import unicodedata

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
# Words that say what kind of place it is, not which one. Ignored when comparing names (unless nothing else is left).
FILLER = {"the", "and", "of", "restaurant", "restaurants", "bar", "grill", "grille", "kitchen", "cafe", "bistro",
          "eatery", "house", "co", "company", "inc", "llc"}
TOWN_ABBREVIATIONS = {"st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort"}

NAME_THRESHOLD = 0.8      # a venue scoring below this is not a candidate
NAME_MARGIN = 0.05        # the best candidate must lead the runner-up by this much, or the name is ambiguous
TOWN_THRESHOLD = 0.85
LIST_LIMIT = 25


class MatchError(Exception):
    """The venue could not be identified; the message says why and what to type instead."""


def _words(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’`]", "", text.replace("&", " and "))
    return re.findall(r"[a-z0-9]+", text)


def _core(text, town_words=()):
    """The words that identify a restaurant: no filler, no town name (unless that would leave nothing)."""
    words = _words(text)
    core = [w for w in words if w not in FILLER and w not in town_words] \
        or [w for w in words if w not in FILLER] or words
    return core


def _similar(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def name_score(query, venue_name, town=""):
    """0 to 1: how well a typed name fits a saved name. 0.8 and up counts as a match."""
    town_words = set(_words(town))
    qf, vf = "".join(_words(query)), "".join(_words(venue_name))
    if not qf or not vf:
        return 0.0
    if qf == vf:
        return 1.0
    qc, vc = _core(query, town_words), _core(venue_name, town_words)
    qk, vk = "".join(qc), "".join(vc)
    if qk == vk:
        return 0.97
    best = 0.0
    short, long_ = sorted((qk, vk), key=len)
    if len(short) >= 4 and short in long_:
        best = 0.9                                   # "Chipotle" for "Chipotle Mexican Grill"
    matched = sum(1 for t in qc if any(t == u or (min(len(t), len(u)) >= 4 and _similar(t, u) >= 0.85) for u in vc))
    best = max(best, 0.9 * matched / max(len(qc), len(vc)))   # same words in another order, or with a typo
    ratio = max(_similar(qk, vk), _similar(qf, vf))   # with and without the filler words
    if ratio >= 0.85:
        best = max(best, 0.95 * ratio)               # a misspelling
    return best


def normalize_state(text):
    """'IL', 'il', 'Illinois' and 'Ill.' style input to a two-letter code, or None."""
    letters = " ".join(_words(text))
    if len(letters) == 2 and letters.upper() in STATES.values():
        return letters.upper()
    return STATES.get(letters)


def _town_key(text):
    text = (text or "").split(",")[0]               # "Naperville, IL" typed into the town box
    return " ".join(TOWN_ABBREVIATIONS.get(w, w) for w in _words(text))


def _listing(names):
    names = sorted(set(names), key=str.lower)
    shown = ", ".join(names[:LIST_LIMIT])
    return shown + (f", and {len(names) - LIST_LIMIT} more" if len(names) > LIST_LIMIT else "")


def find_venue(venues, name, town, state):
    """Return the one saved venue that the name, town and state point to, or raise MatchError."""
    name, town = (name or "").strip(), (town or "").strip()
    if not name or not town or not (state or "").strip():
        raise MatchError("Restaurant name, town and state are all required.")
    code = normalize_state(state)
    if not code:
        raise MatchError(f"I do not recognize the state '{state.strip()}'. Use the two-letter code (IL) or the full name (Illinois).")

    in_state = [v for v in venues if not v.get("state") or v["state"].upper() == code]
    town_key = _town_key(town)
    towns = {v["city"] for v in in_state if v.get("city")}
    scored = {c: (1.0 if _town_key(c) == town_key else _similar(_town_key(c), town_key)) for c in towns}
    top = max(scored.values(), default=0.0)
    if top < TOWN_THRESHOLD:
        known = _listing(towns) or "none"
        raise MatchError(f"No venues are saved in {town}, {code}. Towns on the map in {code}: {known}.")
    matched_towns = {c for c, s in scored.items() if s == top}
    candidates = [v for v in in_state if v.get("city") in matched_towns]
    place = ", ".join(sorted(matched_towns)) + f", {code}"

    ranked = sorted(((name_score(name, v["name"], v.get("city", "")), v) for v in candidates),
                    key=lambda sv: (-sv[0], sv[1]["name"].lower()))
    good = [(s, v) for s, v in ranked if s >= NAME_THRESHOLD]
    if not good:
        raise MatchError(f"No venue in {place} matches '{name}'. Venues saved there: {_listing(v['name'] for v in candidates)}.")
    if len(good) > 1 and good[0][0] - good[1][0] < NAME_MARGIN:
        close = [v["name"] for s, v in good if good[0][0] - s < NAME_MARGIN]
        raise MatchError(f"'{name}' fits more than one venue in {place}: {_listing(close)}. "
                         "Type the full name of the one you mean. If they share the same name, edit venues.json directly.")
    return good[0][1]
