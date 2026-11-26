"""Chains the grid and city searches never add: sit-down chains and national or large regional fast-casual chains. (The
plain fast-food chains are in add_city.BLOCKED_CHAINS.)

The names live in excluded-chains.json, one string per chain; edit that file to add or drop one. A place matches when its
name, read as words, starts with a listed chain's words (a leading "The" is ignored on both sides). Case, apostrophes,
punctuation and "&" versus "and" make no difference, and anything after the chain's name is ignored, so "Chili's Grill &
Bar", "Chilis - Bolingbrook" and "Home Run Inn Pizza - Bolingbrook, IL" all match. Matching at the start of the name keeps a
local place such as "Maple Garden" from matching "Olive Garden".

An entry that starts with "=" must match the whole name. Use it for a short name that a local restaurant could begin with:
"=CAVA" matches "CAVA" and "Cava", but not "Cava Tapas Bar". The chain's own longer spelling can be listed as well ("CAVA Mezze").

add_city.eligible() calls is_chain(), so a chain never becomes a candidate. Adding a venue by hand with the Add Venue form
is not checked: that is a decision by a person."""
import os
import re
import json

CHAINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "excluded-chains.json")


def words(name):
    """The name as lower-case words: apostrophes dropped, "&" read as "and", other punctuation as a space, leading "the" removed."""
    text = re.sub(r"['’‘`]", "", (name or "").lower()).replace("&", " and ")
    found = re.sub(r"[^a-z0-9]+", " ", text).split()
    return found[1:] if found[:1] == ["the"] else found


def load_chains(path=None):
    """[(words of a chain's name, exact), ...] from the chains file. [] when the file cannot be read, so a missing file never
    stops a search (it only stops the ban)."""
    try:
        with open(path or CHAINS_FILE) as f:
            entries = json.load(f)
        out = []
        for entry in entries:
            exact = str(entry).startswith("=")
            w = words(str(entry).lstrip("="))
            if w:
                out.append((w, exact))
        return out
    except (OSError, ValueError, TypeError):
        return []


_chains = None


def is_chain(name, chains=None):
    """True when the place's name starts with a listed chain's name (or equals it, for an "=" entry)."""
    global _chains
    if chains is None:
        if _chains is None:
            _chains = load_chains()
        chains = _chains
    given = words(name)
    return any(given == w if exact else given[:len(w)] == w for w, exact in chains)
