"""Remove a venue named in an issue titled like "Remove Subko".

The venue leaves venues.json and urls.py, and goes on excluded-venues.json so the Add a City importer does not bring it
back. The optional issue body is saved as the reason. Results go to GITHUB_OUTPUT: summary, and error on failure.
"""
import json
import os
import re
import sys

from add_city import name_key, set_output

VENUES_FILE = "venues.json"
URLS_FILE = "urls.py"
EXCLUDED_FILE = "excluded-venues.json"
TITLE_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*:?\s*)?(?:remove|delete)\b\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)


def fail(message):
    set_output("error", message)
    print(message, file=sys.stderr)
    sys.exit(1)


def main():
    match = TITLE_RE.match(os.environ.get("ISSUE_TITLE", ""))
    name = match.group(1).strip() if match else ""
    if not name:
        fail("The title should read like 'Remove <restaurant name>'.")
    key = name_key(name)

    with open(VENUES_FILE) as f:
        text = f.read()
    venues = json.loads(text)
    found = [v for v in venues if name_key(v["name"]) == key] or [v for v in venues if key and key in name_key(v["name"])]
    if not found:
        fail(f"No venue on the map matches '{name}'.")
    if len(found) > 1:
        fail(f"'{name}' matches more than one venue ({', '.join(v['name'] for v in found)}); use the full name.")
    venue = found[0]

    # Cut the entry out of the text so the rest of the file keeps its formatting
    pattern = r',?\n  \{\n    "id": %d,.*?\n  \}' % venue["id"]
    text, count = re.subn(pattern, "", text, count=1, flags=re.DOTALL)
    if count != 1:
        fail(f"Could not find {venue['name']} in {VENUES_FILE} to remove.")
    json.loads(text)
    with open(VENUES_FILE, "w") as f:
        f.write(text)

    if os.path.exists(URLS_FILE):
        with open(URLS_FILE) as f:
            lines = f.readlines()
        with open(URLS_FILE, "w") as f:
            f.writelines(l for l in lines if not re.match(r"\s*%d:\s" % venue["id"], l))

    reason = re.sub(r"\s+", " ", os.environ.get("ISSUE_BODY") or "").strip() or "removed by request"
    with open(EXCLUDED_FILE) as f:
        excluded = json.load(f)
    if not any(name_key(e["name"]) == name_key(venue["name"]) for e in excluded):
        excluded.append({"name": venue["name"], "reason": reason})
        with open(EXCLUDED_FILE, "w") as f:
            json.dump(excluded, f, indent=2, ensure_ascii=False)
            f.write("\n")

    set_output("summary", f"✓ {venue['name']} was removed from the map and added to excluded-venues.json ({reason}).")


if __name__ == "__main__":
    main()
