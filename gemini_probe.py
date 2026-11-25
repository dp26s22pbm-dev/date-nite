"""One-click probe of Gemini's Google Search grounding and thinking settings (workflow: Gemini Probe).

Since Nov 4 every venue that reached the importer's Gemini web search logged `search_gemini no_sources: no grounding
metadata in the response`, and on Nov 25 thinking tokens were about 93% of the output tokens Gemini billed. This runs the
importer's own search prompt for one venue a few ways and prints, for each, how the response ended, what kinds of parts it
held (tool_call / tool_response parts are where a newer model may put search results), whether grounding metadata came
back, what it cost, and the first words of the reply:

  as the importer       google_search tool, temperature 0.1, default thinking
  thinking LOW          the same with thinking_level LOW
  thinking MINIMAL      the same with thinking_level MINIMAL
  thinking budget 0     the same with thinking_budget 0 (a model that does not allow it answers with an error, shown as such)
  no temperature        the same as the importer, without the temperature setting

The calls go through gemini_usage, so they are counted in the weekly budget, and RUN_BUDGET_USD caps the probe.
Environment: GEMINI_API_KEY, PROBE_VENUE, PROBE_CITY."""
import os
import json
import time

from google.genai import types

import gemini_usage
import add_venue_from_issue as imp


def variants():
    search = [types.Tool(google_search=types.GoogleSearch())]
    cfg = lambda **kw: types.GenerateContentConfig(tools=search, **kw)
    return [
        ("as the importer", cfg(temperature=0.1)),
        ("thinking LOW", cfg(temperature=0.1, thinking_config=types.ThinkingConfig(thinking_level="LOW"))),
        ("thinking MINIMAL", cfg(temperature=0.1, thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"))),
        ("thinking budget 0", cfg(temperature=0.1, thinking_config=types.ThinkingConfig(thinking_budget=0))),
        ("no temperature", cfg()),
    ]


def describe(response):
    """A dict of what the response held."""
    out = {"shape": imp.response_shape(response), "grounding": imp.grounding_summary(response)}
    try:
        meta = response.usage_metadata
        out["tokens"] = {"in": meta.prompt_token_count, "out": meta.candidates_token_count, "think": meta.thoughts_token_count,
                         "tool_use_prompt": getattr(meta, "tool_use_prompt_token_count", None)}
    except Exception:
        out["tokens"] = None
    extra = []
    for part in getattr(getattr(response.candidates[0], "content", None), "parts", None) or []:
        for kind in ("tool_call", "tool_response"):
            value = getattr(part, kind, None)
            if value:
                extra.append(f"{kind}: {str(value)[:240]}")
    out["tool_parts"] = extra[:4]
    out["reply"] = (getattr(response, "text", None) or "")[:240].replace("\n", " ")
    return out


def main():
    venue, city = os.environ.get("PROBE_VENUE") or "Traverso's Restaurant", os.environ.get("PROBE_CITY") or "Wheaton, IL"
    client = gemini_usage.TrackedClient(imp.client._client)
    prompt = imp.SEARCH_PRICING_PROMPT.format(venue_name=venue, city=city, scope_rules=imp.SCOPE_RULES)
    results = []
    for label, config in variants():
        started = time.time()
        try:
            response = client.models.generate_content(model="gemini-3.6-flash", contents=prompt, config=config)
            row = {"variant": label, **describe(response)}
        except Exception as e:
            row = {"variant": label, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        row["seconds"] = round(time.time() - started, 1)
        results.append(row)
        print(json.dumps(row, indent=2, default=str))
    lines = [f"### Gemini probe: {venue}, {city}", "", "| Variant | Grounding | Tokens (in / out / thinking) | Ended as | Reply starts |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['variant']} | error | | {r['error'].replace('|', '/')} | |")
            continue
        t = r.get("tokens") or {}
        grounded = "yes" if "search queries=" in r["grounding"] and "queries=[]" not in r["grounding"] else "no"
        lines.append(f"| {r['variant']} | {grounded} | {t.get('in')} / {t.get('out')} / {t.get('think')} | {r['shape'].replace('|', '/')} | {r['reply'].replace('|', '/')[:80]} |")
    summary = "\n".join(lines)
    print(summary)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(summary + "\n")


if __name__ == "__main__":
    main()
