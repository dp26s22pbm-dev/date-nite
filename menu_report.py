"""Weekly analysis of menu import failures, written as markdown for a GitHub issue.

    python menu_report.py [--days 7] [--out weekly-report.md]

Reads the menu-log branch (see menu_log.py), compares the last N days with the N days before, and has Claude look for
patterns in the failures and recommend importer changes. The numbers come from menu_log.report and are always included.
If ANTHROPIC_API_KEY is missing or the request fails, the report is just the numbers and says so; this never fails the
workflow over the analysis.

Claude sees counts, hosts, platforms and step results from the log. The log holds no menu text or page HTML, so the
recommendations are hypotheses to check against the venue's real menu page, and the prompt says so."""
import json
import os
import sys
from collections import Counter

import menu_log

MODEL = os.environ.get("REPORT_MODEL", "claude-sonnet-5-5")
MAX_VENUES = 40  # failed imports sent to Claude with their step trail; the rest are covered by the counts

PROMPT = """You are reviewing the weekly failure log of a menu importer for "Date Nite", a map of restaurants with an
estimated date-night cost. For each venue the importer tries, in order: pasted menu text, the restaurant's own site (plain
request, then a headless browser), linked PDF/image menus, Gemini reading the site, Gemini web search, then Claude web
search. An outcome of menu_read is a full success; menu_read_partial invented some categories; web_search_only,
none, reply_unreadable and incomplete leave the venue without a real menu. reply=true means a person pasted the menu.

Below are this week's numbers, last week's outcome counts, and the step trail of each failed import. Write a short report
in markdown with these sections:

## Patterns
Group the failures by cause (a platform, an HTTP status, a step that fails everywhere, a type of venue). Give counts and
name the venues or hosts. Say which patterns are new or growing compared with last week.

## Recommendations
At most five, ranked by how many venues each would fix. For each: the failure it addresses (use the step: result words),
the change to try, and the part of add_venue_from_issue.py or menu_log.py it would touch if you can tell from the trail.

## Needs a person
Venues that probably need a pasted menu or a manual look.

Rules: use only what the data shows. The log has no menu text, so mark anything that is a guess about why a page failed
as a guess, and say what to look at to confirm it. If a week is too small to show a pattern, say so instead of inventing
one. Do not pad. No em dashes.

THIS WEEK'S REPORT
{report}

OUTCOME COUNTS, LAST WEEK
{previous}

FAILED IMPORTS THIS WEEK (JSON, one per line)
{failed}
"""


def trail(entry):
    """One failed import boiled down to what a reader needs: the venue, how it ended, and each non-ok step."""
    steps = [{k: s[k] for k in ("step", "result", "host", "platform", "n") if s.get(k) is not None}
             for s in entry.get("steps", []) if s.get("result") != "ok"]
    return {"venue": entry.get("venue"), "host": entry.get("host"), "website_status": entry.get("website_status"),
            "outcome": entry.get("outcome"), "reply": bool(entry.get("reply")), "source": entry.get("source"),
            "defaulted": entry.get("defaulted"), "steps": steps}


def outcome_counts(entries):
    counts = Counter(e.get("outcome") for e in entries)
    return ", ".join(f"{k}: {n}" for k, n in counts.most_common()) or "no imports"


def analyze(report_text, previous, failed):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, "ANTHROPIC_API_KEY is not set"
    try:
        import anthropic
        reply = anthropic.Anthropic(api_key=api_key).messages.create(
            model=MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": PROMPT.format(report=report_text, previous=previous, failed=failed)}])
        text = "".join(b.text for b in reply.content if b.type == "text").strip()
        return (text, None) if text else (None, "Claude returned no text")
    except Exception as exc:  # the numbers are still worth posting
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


def build(entries, days=7):
    this_week = menu_log.within_days(entries, days)
    last_week = menu_log.within_days(entries, days, offset=days)
    if not this_week:
        return f"# Menu import report\n\nNo menu imports were logged in the last {days} days."

    report_text = menu_log.report(this_week)
    failed = [e for e in this_week if e.get("outcome") != "menu_read"]
    failed_lines = "\n".join(json.dumps(trail(e), ensure_ascii=False) for e in failed[-MAX_VENUES:]) or "(none)"
    previous = outcome_counts(last_week)

    head = [f"# Menu import report, last {days} days", "",
            f"**This week:** {len(this_week)} imports ({outcome_counts(this_week)})  ",
            f"**Week before:** {len(last_week)} imports ({previous})", ""]
    if not failed:
        return "\n".join(head + ["Every import this week read a full menu. Nothing to fix."])

    analysis, problem = analyze(report_text, previous, failed_lines)
    body = head
    if analysis:
        body += [analysis, ""]
    else:
        body += [f"_Pattern analysis was skipped ({problem}). The numbers are below._", ""]
    if len(failed) > MAX_VENUES:
        body += [f"_Analysis covered the latest {MAX_VENUES} of {len(failed)} failed imports._", ""]
    body += ["<details><summary>Full numbers</summary>", "", "```", report_text, "```", "", "</details>"]
    return "\n".join(body)


def main():
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "weekly-report.md"
    text = build(menu_log.load_entries(), days)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
