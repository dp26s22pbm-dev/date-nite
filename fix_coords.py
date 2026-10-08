import json
import re
import urllib.request

# Verified Google Maps share links or exact [longitude, latitude] arrays
# Ancho & Agave verified at 95th & 59: [-88.205216, 41.712331]
VENUE_LOCATION_MAP = {
    "Ancho & Agave": [-88.197584, 41.714968],
    # Add your other existing venues here:
    # "Allegory": "https://maps.app.goo.gl/...",
    # "Santo Cielo": "https://maps.app.goo.gl/...",
    # "Vasili's": "https://maps.app.goo.gl/...",
}

def extract_coords_from_google_maps_url(input_val):
    if isinstance(input_val, list) and len(input_val) == 2:
        return [round(float(input_val[0]), 6), round(float(input_val[1]), 6)]

    input_str = str(input_val).strip()
    raw_nums = re.findall(r'[-+]?\d+\.\d+', input_str)
    if len(raw_nums) >= 2:
        val1, val2 = float(raw_nums[0]), float(raw_nums[1])
        lat = val1 if val1 > 0 else val2
        lon = val2 if val2 < 0 else val1
        return [round(lon, 6), round(lat, 6)]

    final_url = input_str
    if "maps" in final_url or "goo.gl" in final_url:
        try:
            req = urllib.request.Request(
                final_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                final_url = resp.geturl()
        except Exception as e:
            print(f"Failed expanding map link: {e}")

    match = re.search(r'[@\?q=]([-+]?\d+\.\d+),([-+]?\d+\.\d+)', final_url)
    if match:
        val1, val2 = float(match.group(1)), float(match.group(2))
        lat = val1 if val1 > 0 else val2
        lon = val2 if val2 < 0 else val1
        return [round(lon, 6), round(lat, 6)]

    match_proto = re.search(r'!3d([-+]?\d+\.\d+)!4d([-+]?\d+\.\d+)', final_url)
    if match_proto:
        lat = float(match_proto.group(1))
        lon = float(match_proto.group(2))
        return [round(lon, 6), round(lat, 6)]

    return None

def update_all_venues():
    with open("venues.json", "r") as f:
        venues = json.load(f)

    updated_count = 0
    for v in venues:
        name = v.get("name")
        if name in VENUE_LOCATION_MAP:
            new_coords = extract_coords_from_google_maps_url(VENUE_LOCATION_MAP[name])
            if new_coords:
                v["coords"] = new_coords
                print(f"Updated {name} -> {new_coords}")
                updated_count += 1
            else:
                print(f"FAILED to parse coords for {name}")

    with open("venues.json", "w") as f:
        json.dump(venues, f, indent=2)

    print(f"\nDone: {updated_count} venues updated in venues.json.")

if __name__ == "__main__":
    update_all_venues()
