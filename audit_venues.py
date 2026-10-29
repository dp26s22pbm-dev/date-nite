"""Audit the venues on the map against Google Places, to test a sit-down rule before add_city.py uses one.

Each venue in venues.json is looked up by name near its saved pin (one Text Search request per venue). The place
Google returns is matched to the venue by name and distance, then three candidate rules are applied to its fields. The
report lists how many venues each rule would have excluded, which ones, and how often Google leaves the fields blank,
so the excluded names can be checked by eye against restaurants that are known to be sit-down.

Nothing is written to venues.json. Writes audit-venues.csv (override with AUDIT_CSV) and, on GitHub Actions, the report
to the run summary. AUDIT_LIMIT=N audits only the first N venues, for a cheap first run."""
import os
import sys
import csv
import json
import math
import time
import urllib.request
import urllib.error

import places_usage
from add_city import name_key

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join([
    "places.displayName", "places.location", "places.primaryType", "places.types", "places.priceLevel",
    "places.dineIn", "places.reservable", "places.takeout", "places.delivery", "places.servesBeer", "places.servesWine",
])
# A place must sit this close to the saved pin when its name matches, or this close when only its location does
NAME_MATCH_M = 400
LOCATION_ONLY_M = 75

def haversine_m(a, b):
    """Metres between two [lng, lat] points."""
    lng1, lat1, lng2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

def search(venue, api_key):
    """Places near the venue's saved pin that match its name, as Google returns them (up to 5)."""
    lng, lat = venue["coords"]
    payload = {"textQuery": venue["name"], "pageSize": 5,
               "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 500.0}}}
    req = urllib.request.Request(PLACES_URL, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELDS})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            places_usage.record(FIELDS, 200)
            return data.get("places") or []
        except urllib.error.HTTPError as e:
            places_usage.record(FIELDS, e.code)
            body = e.read().decode(errors="replace")[:300]
            if e.code == 429 and attempt == 1:
                time.sleep(5)
                continue
            if e.code in (400, 403):  # a bad field name or key would fail every venue the same way
                sys.exit(f"Places answered HTTP {e.code}: {body}")
            raise

def pick(venue, places):
    """(place, distance in m, how it matched) for the best candidate, or (None, None, 'no match')."""
    key = name_key(venue["name"])
    scored = []
    for p in places:
        loc = p.get("location") or {}
        if "latitude" not in loc:
            continue
        pkey = name_key((p.get("displayName") or {}).get("text", ""))
        scored.append((haversine_m(venue["coords"], [loc["longitude"], loc["latitude"]]), pkey, p))
    named = sorted((s for s in scored if s[1] and (s[1] in key or key in s[1]) and s[0] <= NAME_MATCH_M), key=lambda s: s[0])
    if named:
        return named[0][2], round(named[0][0]), "name and location"
    near = sorted((s for s in scored if s[0] <= LOCATION_ONLY_M), key=lambda s: s[0])
    if near:
        return near[0][2], round(near[0][0]), "location only"
    return None, None, "no match"

def alcohol(p):
    return bool(p.get("servesBeer") or p.get("servesWine"))

# Each rule answers True when it would keep the place. A field Google leaves out is None, never False.
RULES = {
    "A: drop only if dineIn is false": lambda p: p.get("dineIn") is not False,
    "B: require reservable": lambda p: p.get("reservable") is True,
    "C: drop if dineIn is false, or cheap with no reservations and no beer or wine":
        lambda p: not (p.get("dineIn") is False or (p.get("priceLevel") == "PRICE_LEVEL_INEXPENSIVE"
                                                    and p.get("reservable") is not True and not alcohol(p))),
}

def show(value):
    return "" if value is None else str(value)

def audit(venues, api_key, delay=0.2):
    rows = []
    for i, venue in enumerate(venues, 1):
        place, metres, how = pick(venue, search(venue, api_key))
        row = {"name": venue["name"], "match": how, "metres": show(metres)}
        if place:
            row.update({"google_name": (place.get("displayName") or {}).get("text", ""), "primaryType": show(place.get("primaryType")),
                        "priceLevel": show(place.get("priceLevel")).replace("PRICE_LEVEL_", ""),
                        **{f: show(place.get(f)) for f in ("dineIn", "reservable", "takeout", "delivery", "servesBeer", "servesWine")}})
            for rule, keep in RULES.items():
                row[rule] = "keep" if keep(place) else "EXCLUDE"
        rows.append(row)
        print(f"{i}/{len(venues)} {venue['name']}: {how}")
        time.sleep(delay)
    return rows

def report(rows):
    matched = [r for r in rows if r["match"] != "no match"]
    lines = ["## Venue audit\n", f"{len(rows)} venues looked up, {len(matched)} matched to a Google place "
             f"({sum(r['match'] == 'location only' for r in matched)} by location only).\n"]
    lines.append("### How often Google leaves a field blank (matched venues)\n")
    lines.append("| Field | Blank | True | False |\n|---|---|---|---|")
    for f in ("dineIn", "reservable", "takeout", "delivery", "servesBeer", "servesWine"):
        blank = sum(r[f] == "" for r in matched)
        true = sum(r[f] == "True" for r in matched)
        lines.append(f"| {f} | {blank} | {true} | {len(matched) - blank - true} |")
    lines.append(f"| priceLevel | {sum(r['priceLevel'] == '' for r in matched)} | set: {sum(r['priceLevel'] != '' for r in matched)} | |")
    lines.append("\n### Venues each rule would have excluded\n")
    for rule in RULES:
        out = [r for r in matched if r[rule] == "EXCLUDE"]
        lines.append(f"**{rule}**: {len(out)} of {len(matched)} excluded")
        lines += [f"- {r['name']} (dineIn {r['dineIn'] or '?'}, reservable {r['reservable'] or '?'}, price {r['priceLevel'] or '?'})" for r in out]
        lines.append("")
    unmatched = [r["name"] for r in rows if r["match"] == "no match"]
    if unmatched:
        lines += ["### No Google match (not counted above)\n"] + [f"- {n}" for n in unmatched]
    return "\n".join(lines)

def main():
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not api_key:
        sys.exit("GOOGLE_PLACES_API_KEY is not set")
    with open("venues.json") as f:
        venues = json.load(f)
    limit = int(os.environ.get("AUDIT_LIMIT") or 0)
    if limit:
        venues = venues[:limit]
    rows = audit(venues, api_key)
    columns = ["name", "match", "metres", "google_name", "primaryType", "priceLevel", "dineIn", "reservable", "takeout",
               "delivery", "servesBeer", "servesWine", *RULES]
    with open(os.environ.get("AUDIT_CSV", "audit-venues.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)
    text = report(rows)
    print("\n" + text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")

if __name__ == "__main__":
    main()
