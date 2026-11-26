"""Second Gemini probe (workflow: Gemini Probe, probe = search-and-thinking). The first one showed that Gemini accepts the
google_search tool but never runs it (plain text back, no grounding metadata, no tool parts), and that lowering the thinking
level removes most of the output tokens. This asks two things.

Part 1, does search work at all, and what turns it on:
  models         lists the Gemini models the key can use, so the other tests can try a second and third Flash model
  control        a question that needs a live search (Chicago weather), on the importer's model and on up to two other Flash models
  prose prompt   the venue question as a plain request, without the importer's "output pure JSON, no explanations" ending
  search_types   the importer's prompt with GoogleSearch(search_types=web_search)
  server-side    the importer's prompt, and the prose prompt, with tool_config include_server_side_tool_invocations=True
  function ANY   the importer's prompt with function_calling_config mode ANY (the API may reject this; the error is the answer)

Part 2, does a lower thinking level change what is read from a menu:
  The importer's own extraction call (PDF_PROMPT plus menu text) on a made-up menu whose right answers are known, with the
  default thinking (twice, to show run-to-run noise), LOW, MINIMAL and budget 0. Each reply is graded against the known lists:
  how many of the real prices it found, and whether it let in the items the prompt says to leave out (a tomahawk for two, a
  seafood tower, soft drinks).

Calls go through gemini_usage (counted in the weekly budget); RUN_BUDGET_USD caps the probe. A grounded search is billed per
query and the model chooses how many (the prose prompt ran 8, an estimated $0.28), so part 1 can cost most of a dollar; part 2
makes five cheap calls. PROBE_PART picks "thinking" (part 2 only), "search" (part 1 only) or "both" (part 2 first).
Environment: GEMINI_API_KEY, PROBE_VENUE, PROBE_CITY, PROBE_PART."""
import os
import re
import json
import time
from collections import Counter

from google.genai import types

import gemini_usage
import add_venue_from_issue as imp

MODEL = "gemini-3.6-flash"
SEARCH = lambda **kw: [types.Tool(google_search=types.GoogleSearch(**kw))]

# A made-up menu (not a real restaurant) with the answers worked out by the rules in PDF_PROMPT
MENU = """HARBOR & VINE  |  Dinner

STARTERS
Crispy Calamari  14
Burrata & Heirloom Tomato  15
French Onion Soup  9
Wedge Salad  11
Shrimp Cocktail  17
Truffle Fries  8

HANDHELDS, PASTA & PIZZA
Harbor Burger  17
Fried Chicken Sandwich  16
Rigatoni Bolognese  21
Margherita Pizza  18
Chicken Marsala  24
Shrimp Scampi Linguine  26

FROM THE GRILL & THE SEA
Filet Mignon, 8 oz  49
Ribeye, 14 oz  52
New York Strip, 12 oz  46
Pan-Seared Salmon  34
Chilean Sea Bass  44
Maine Lobster Tail  58
Tomahawk for Two  135
Seafood Tower, serves four  85

COCKTAILS
Old Fashioned  14
Espresso Martini  15
Margarita  13
Negroni  14
Moscow Mule  12
Aperol Spritz  13

BEVERAGES
Fountain Soda  4
Fresh Lemonade  5

DESSERTS
Tiramisu  10
Cheesecake  11
Creme Brulee  10
Gelato  8
Chocolate Cake  11
"""
EXPECTED = {"apps": [14, 15, 9, 11, 17, 8], "casualMains": [17, 16, 21, 18, 24, 26],
            "premiumMains": [49, 52, 46, 34, 44, 58], "drinks": [14, 15, 13, 14, 12, 13], "desserts": [10, 11, 10, 8, 11]}
LEFT_OUT = {"premiumMains": [135, 85], "drinks": [4, 5]}


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return f"unreadable: {e}"


def grounded(response):
    """(searched, queries, source pages). Queries come from gemini_usage.search_queries_in (grounding metadata or, with
    include_server_side_tool_invocations, GOOGLE_SEARCH tool_call parts); pages from the grounding metadata."""
    chunks = 0
    for cand in getattr(response, "candidates", None) or []:
        chunks += len(getattr(getattr(cand, "grounding_metadata", None), "grounding_chunks", None) or [])
    queries = gemini_usage.search_queries_in(response)
    return queries > 0 or chunks > 0, queries, chunks


def tool_parts(response):
    out = []
    for cand in (getattr(response, "candidates", None) or [])[:1]:
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            for kind in ("tool_call", "tool_response"):
                if getattr(part, kind, None):
                    out.append(f"{kind}: {str(getattr(part, kind))[:160]}")
    return out[:3]


def usage(response):
    meta = getattr(response, "usage_metadata", None)
    return {"in": getattr(meta, "prompt_token_count", None), "out": getattr(meta, "candidates_token_count", None),
            "think": getattr(meta, "thoughts_token_count", None)}


def flash_models(client):
    """(all Gemini model names the key lists, up to two other stable Flash models to try). Empty lists when the listing fails."""
    try:
        names = sorted({m.name.split("/")[-1] for m in client.models.list() if "gemini" in (m.name or "")})
    except Exception as e:
        return [f"listing failed: {type(e).__name__}: {str(e)[:200]}"], []
    stable = [n for n in names if re.fullmatch(r"gemini-[\d.]+-flash(-lite)?", n) and n != MODEL]
    stable.sort(key=lambda n: [int(x) for x in re.findall(r"\d+", n)], reverse=True)
    return names, stable[:2]


def search_calls(client, models, venue, city):
    importer_prompt = imp.SEARCH_PRICING_PROMPT.format(venue_name=venue, city=city, scope_rules=imp.SCOPE_RULES)
    prose = (f"Find the dinner menu for {venue} in {city}. Search the web for its current menu and tell me what its appetizers, "
             "main courses, cocktails and desserts cost. Name the pages you used.")
    control = "What is the weather in Chicago, Illinois right now? Use Google Search and name the pages you used."
    server_side = types.ToolConfig(include_server_side_tool_invocations=True)
    any_call = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY"))
    plan = [("control, importer's model", MODEL, control, types.GenerateContentConfig(tools=SEARCH())),
            ("prose prompt", MODEL, prose, types.GenerateContentConfig(tools=SEARCH())),
            ("importer prompt + search_types", MODEL, importer_prompt,
             types.GenerateContentConfig(tools=SEARCH(search_types=types.SearchTypes(web_search=types.WebSearch())), temperature=0.1)),
            ("importer prompt + server-side invocations", MODEL, importer_prompt,
             types.GenerateContentConfig(tools=SEARCH(), temperature=0.1, tool_config=server_side)),
            ("prose prompt + server-side invocations", MODEL, prose,
             types.GenerateContentConfig(tools=SEARCH(), tool_config=server_side)),
            ("importer prompt + function mode ANY", MODEL, importer_prompt,
             types.GenerateContentConfig(tools=SEARCH(), temperature=0.1, tool_config=any_call))]
    plan += [(f"control, {m}", m, control, types.GenerateContentConfig(tools=SEARCH())) for m in models]
    rows = []
    for label, model, prompt, config in plan:
        started = time.time()
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
            searched, queries, chunks = grounded(response)
            row = {"test": label, "model": model, "searched": searched, "queries": queries, "pages": chunks,
                   "tool_parts": tool_parts(response), "usage": usage(response), "shape": safe(imp.response_shape, response),
                   "reply": (getattr(response, "text", None) or "")[:160].replace("\n", " ")}
        except Exception as e:
            row = {"test": label, "model": model, "error": f"{type(e).__name__}: {str(e)[:260]}"}
        row["seconds"] = round(time.time() - started, 1)
        rows.append(row)
        print(json.dumps(row, indent=2, default=str))
    return rows


def grade(data):
    """Per category: how many of the known prices were found, how many other prices came back, and whether a left-out price slipped in."""
    out = {}
    for key, want in EXPECTED.items():
        got = Counter(round(float(x)) for x in (data.get(key) or []) if isinstance(x, (int, float)))
        need = Counter(want)
        found = sum(min(got[p], need[p]) for p in need)
        extra = sum(got.values()) - found
        slipped = [p for p in LEFT_OUT.get(key, []) if got[p]]
        out[key] = {"found": f"{found}/{len(want)}", "extra": extra, "slipped": slipped}
    return out


def extraction_calls(client):
    contents = [imp.PDF_PROMPT, "Menu text:\n" + MENU]
    plan = [("default thinking, run 1", None), ("default thinking, run 2", None),
            ("thinking LOW", types.ThinkingConfig(thinking_level="LOW")),
            ("thinking MINIMAL", types.ThinkingConfig(thinking_level="MINIMAL")),
            ("thinking budget 0", types.ThinkingConfig(thinking_budget=0))]
    rows = []
    for label, thinking in plan:
        started = time.time()
        try:
            config = types.GenerateContentConfig(thinking_config=thinking) if thinking else None
            response = client.models.generate_content(model=MODEL, contents=contents, config=config)
            data = json.loads(response.text.strip().replace("```json", "").replace("```", ""))
            row = {"test": label, "usage": usage(response), "grade": grade(data)}
        except Exception as e:
            row = {"test": label, "error": f"{type(e).__name__}: {str(e)[:260]}"}
        row["seconds"] = round(time.time() - started, 1)
        rows.append(row)
        print(json.dumps(row, indent=2, default=str))
    return rows


def summary(names, search_rows, extract_rows, venue, city):
    lines = [f"### Gemini probe 2: {venue}, {city}", ""]
    if search_rows:
        lines += [f"Models the key lists ({len(names)}): " + ", ".join(names[:40]), "",
                  "#### Does search run?", "", "| Test | Searched | Queries | Pages | Tool parts | Tokens in / out / thinking | Result |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in search_rows:
        if "error" in r:
            lines.append(f"| {r['test']} | error | | | | | {r['error'].replace('|', '/')} |")
            continue
        u = r["usage"]
        lines.append(f"| {r['test']} | {'yes' if r['searched'] else 'no'} | {r['queries']} | {r['pages']} | {len(r['tool_parts'])} | "
                     f"{u['in']} / {u['out']} / {u['think']} | {r['reply'].replace('|', '/')[:90]} |")
    if extract_rows:
        lines += ["", "#### Does lower thinking change what is read from a menu?", "",
                  "Prices found of the real ones (apps, casual, premium, drinks, desserts), other prices let in, and left-out items that slipped in.", "",
                  "| Setting | Tokens in / out / thinking | Apps | Casual | Premium | Drinks | Desserts | Slipped in |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in extract_rows:
        if "error" in r:
            lines.append(f"| {r['test']} | error | | | | | | {r['error'].replace('|', '/')} |")
            continue
        g, u = r["grade"], r["usage"]
        cell = lambda k: f"{g[k]['found']} (+{g[k]['extra']})"
        slipped = [str(p) for k in g for p in g[k]["slipped"]]
        lines.append(f"| {r['test']} | {u['in']} / {u['out']} / {u['think']} | {cell('apps')} | {cell('casualMains')} | {cell('premiumMains')} | "
                     f"{cell('drinks')} | {cell('desserts')} | {', '.join(slipped) or 'none'} |")
    return "\n".join(lines)


def main():
    venue, city = os.environ.get("PROBE_VENUE") or "Traverso's Restaurant", os.environ.get("PROBE_CITY") or "Wheaton, IL"
    client = gemini_usage.TrackedClient(imp.client._client)
    part = (os.environ.get("PROBE_PART") or "both").strip().lower()
    # The thinking test makes cheap calls with no searches, so it goes first: a grounded search costs per query (the prose
    # prompt ran 8) and used up the whole budget on Nov 25 before the thinking test started
    extract_rows = extraction_calls(client) if part in ("both", "thinking") else []
    names, search_rows = [], []
    if part in ("both", "search"):
        names, others = flash_models(client)
        print("models:", names, "| other flash models tried:", others)
        search_rows = search_calls(client, others, venue, city)
    text = summary(names, search_rows, extract_rows, venue, city)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
