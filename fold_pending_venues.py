"""Move venues from pending-venues/*.json into venues.json, giving each the next free id.

Add City pull requests save their venues without ids in pending-venues/ so two of them never edit the same part of
venues.json. The fold-venues workflow runs this after such a pull request merges. A venue whose name is already in
venues.json (or earlier in the same run) is dropped, which covers a venue that two neighbouring cities both found."""
import os
import re
import json

PENDING_DIR = "pending-venues"

def name_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())

def main():
    files = sorted(f for f in os.listdir(PENDING_DIR) if f.endswith(".json")) if os.path.isdir(PENDING_DIR) else []
    if not files:
        print("Nothing pending.")
        return
    with open("venues.json") as f:
        venues = json.load(f)
    known = {name_key(v["name"]) for v in venues}
    next_id = max((v.get("id", 0) for v in venues), default=0) + 1
    for name in files:
        path = os.path.join(PENDING_DIR, name)
        with open(path) as f:
            pending = json.load(f)
        for venue in pending:
            if name_key(venue["name"]) in known:
                print(f"{name}: {venue['name']} is already on the map; skipped.")
                continue
            venues.append({"id": next_id, **{k: v for k, v in venue.items() if k != "id"}})
            known.add(name_key(venue["name"]))
            print(f"{name}: added {venue['name']} as id {next_id}.")
            next_id += 1
        os.remove(path)
    with open("venues.json", "w") as f:
        json.dump(venues, f, indent=2)

if __name__ == "__main__":
    main()
