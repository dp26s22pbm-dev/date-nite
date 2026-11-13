# Date Nite

A static site (`index.html`, `venues.json`) mapping restaurants with an estimated "date night" cost. Venues are added by GitHub Actions workflows that read each restaurant's menu and save five prices per venue. This file is mainly for improving that menu importer.

## The menu importer

- `add_venue_from_issue.py` is the importer. It tries sources in order and falls back down the list: pasted menu text, the restaurant's own site (plain request, then a headless Playwright browser), linked PDF/image menus, Gemini reading the site, Gemini web search, then Claude web search (`claude_search_pricing`), after which the venue's Yelp menu page is tried when the search found its Yelp listing (`pricing_from_yelp_menu`). Gemini prompts live in `PDF_PROMPT`, `SCOPE_RULES` and `SEARCH_PRICING_PROMPT` near the top.
- Output per venue: `app`, `casualMain`, `premiumMain`, `drink`, `dessert` (medians of the category lists, with stand-in numbers when a category is empty), total = (app + casualMain + premiumMain + 2*drink + dessert) * 1.30, mapped to a tier color. Taco places count two casual mains.
- `clean_pricing`, `price_count` (needs at least 6 price-like numbers), `price_sanity_warnings` and `pricing_rules.py` hold the cleaning and sanity rules. Change those rather than duplicating logic.
- Workflows that run the importer: `add-venue.yml`, `add-city.yml`, `search-cells.yml`, `add-menu.yml` (someone replies to a `needs-menu` or `double-check` issue with pasted menu text). Each ends with `python menu_log.py publish`.
- Dependencies: `requirements.txt` (google-genai, playwright, anthropic). Keys (Gemini, Anthropic) exist only in Actions secrets, so full end-to-end runs are not possible in a local or cloud session. Test pieces in isolation.

## The failure log (start here when improving the importer)

- `menu_log.py` writes one JSON line per venue per importer run. Lines are appended to `menu-log.jsonl` on the `menu-log` branch, which is kept off `main` so logging never triggers a site rebuild. The record format and the list of step/result words are in the module docstring. Read it before analyzing.
- Read it back:

```bash
git fetch origin menu-log
python menu_log.py report --recent 20    # outcomes, failure reasons, problem sites, venues with no real menu
python menu_log.py export > /tmp/menu-log.jsonl   # raw records for deeper analysis
```

- Key signals: `outcome` (`menu_read`, `menu_read_partial`, `web_search_only`, `none`, `reply_unreadable`, `incomplete`), `steps` (every attempt with `step`, `result`, `host`, `platform`), and `reply: true`, meaning a person pasted the menu, which is ground truth the importer missed.
- The log stores counts and prices, not menu text or page HTML. If a failure cannot be diagnosed from the log alone, ask for the venue's menu URL or pasted text rather than guessing.
- The `Menu Import Log` workflow (`menu-log.yml`) prints the same report from the Actions tab.
- When the importer learns a new way to fail, add a new result word in `add_venue_from_issue.py` via `menu_log.step(...)` and list it in the `menu_log.py` docstring. Logging must never raise or block a venue from being added.

## How to work on importer improvements

1. Run the report, pick the most common failure reason or problem platform, and look at the specific records behind it.
2. Reproduce with the smallest piece you can run without keys (for example `fetch_url`, `html_to_text`, `menu_page_candidates`, `price_count`, `clean_pricing`), or with `menu_prices.py`.
3. `/menu-prices` (`.claude/commands/menu-prices.md`, backed by `menu_prices.py`) turns an uploaded menu into the five prices using the importer's own functions. Use it to check what a corrected extraction should produce.
4. Keep changes narrow and name the failure reason being fixed. Say plainly what could not be tested without API keys.
5. Do not edit `venues.json` directly for importer work unless asked. It is changed by workflows and stamped with version commits.

## Repo notes

- `main` auto-deploys the site (CNAME, GitHub Pages) and `stamp-version.yml` adds "Stamp version N" commits. Do not hand-edit those stamps.
- Other data branches exist, such as `places-usage` (same pattern as `menu-log`).
- `dashboard_stats.py` (workflow `dashboard-stats.yml`, nightly) computes the numbers for the private dashboard page and saves them as `dashboard-stats.json` on the `dashboard-data` branch (one commit, replaced each run). Read it with `git fetch origin dashboard-data && git show origin/dashboard-data:dashboard-stats.json`. It reuses `menu_log.py`, `places_usage.py` and `grid_cells.py`, so change rules there, not in the stats script. Stale venues are those whose price or `lastUpdated` last changed over 30 days ago (`PRICE_FIELDS`).
- Venue add/remove/audit scripts (`add_venue_from_issue.py`, `remove_venue_from_issue.py`, `audit_venues.py`, `batch_update_venues.py`, `fold_pending_venues.py`) are driven by issues.
- Editing a venue already on the map: the "Edit a Venue" issue form (`edit-venue.yml` workflow, `edit_venue_from_issue.py`). Name, town and state are required. `venue_match.py` finds the venue generously (case, punctuation, filler words, typos, town name in the venue name) and refuses an ambiguous name instead of guessing. Pasted menu text (read with the importer's `pricing_from_menu_text`) and `key: price` overrides replace saved prices; blanks keep saved values. It never searches the web, never adds `needs-menu` or `double-check` labels, and always closes the issue. `venue_match.py` is standard-library only, so it can be tested locally without keys. `grid/`, `grid_cells.py`, `add_cell.py`, `split_cells.py` manage the Places search grid.
