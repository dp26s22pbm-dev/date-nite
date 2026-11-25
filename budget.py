"""Weekly spending budget for the AI calls the importer makes, and the gate a scheduled search checks before it starts.

Spend is estimated from what the repository logs: Gemini tokens and grounding queries (gemini_usage.py, on the
gemini-usage branch) and Claude web-search tokens and searches (the search_claude steps in menu-log.jsonl). Google
Places has its own daily request limit (places_usage.py); the gate reports what is left of it and does not price it.

    python budget.py report            this week's spend against the budget, and what today's pace allows
    python budget.py gate              the same numbers as key=value lines for a workflow (also written to $GITHUB_OUTPUT)

Settings (environment variables):
    WEEKLY_BUDGET_USD       required for the gate. Unset or 0 means nothing is allowed.
    BUDGET_ALREADY_SPENT_USD  spend this week that predates the Gemini log, so the first week is not undercounted
    BUDGET_RATES_JSON       JSON that replaces entries of RATES (the same shape)
    PER_VENUE_USD_DEFAULT   cost of one imported venue when there is too little history to measure it (default 0.10)
    BUDGET_SAFETY           fraction of the day's remaining allowance the gate hands out (default 0.8)
    MAX_CELLS               ceiling on cells per run (default 5, the same variable add_cell.py reads)

The week runs Monday 00:00 to Sunday 24:00 Pacific time. The day's allowance is what is left of the week divided by the
days left (today included), so unused money rolls forward and a heavy day shrinks the days after it. The gate prints the
dollars it hands out as run_budget_usd; pass that to the run as RUN_BUDGET_USD and gemini_usage.py stops the run's Gemini
calls when it is spent.

The gate stops (proceed=false) when a log branch cannot be reached, because a spend it cannot see is not spend it can
pace. A branch that does not exist yet counts as nothing spent."""
import os
import sys
import json
import math
import datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

import gemini_usage
import menu_log
import places_usage

PACIFIC = ZoneInfo("America/Los_Angeles")

# US dollars. Tokens are per million; searches are per query. Replace any entry with BUDGET_RATES_JSON.
RATES = {
    "gemini": {
        "default": {"in": 0.30, "out": 2.50},
        "search_each": 0.035,
    },
    "claude": {
        "default": {"in": 3.00, "out": 15.00},
        "search_each": 0.010,
    },
}

HISTORY_DAYS = 28
MIN_HISTORY_VENUES = 5
CELL_VENUES = 3  # a cell is searched until it holds this many venues (grid.json fullAt)


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _load_rate_overrides():
    raw = os.environ.get("BUDGET_RATES_JSON", "").strip()
    if raw:
        try:
            _merge(RATES, json.loads(raw))
        except ValueError:
            print("BUDGET_RATES_JSON is not valid JSON; the built-in rates are used.")


_load_rate_overrides()


def _float_env(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- pricing the logs

def claude_cost(step, rates=None):
    r = (rates or RATES)["claude"]["default"]
    search = (rates or RATES)["claude"]["search_each"]
    return ((step.get("tokens_in") or 0) * r["in"] + (step.get("tokens_out") or 0) * r["out"]) / 1e6 \
        + (step.get("searches") or 0) * search


def spend_events(gemini_entries, menu_entries):
    """[(time, dollars, source)] for every priced call. Failed Gemini calls cost nothing."""
    out = []
    for e in gemini_entries:
        if e.get("ok", True) and e.get("t"):
            out.append((_parse(e["t"]), gemini_usage.entry_cost(e), "gemini"))
    for rec in menu_entries:
        for s in rec.get("steps", []):
            if s.get("step") == "search_claude" and rec.get("t"):
                out.append((_parse(rec["t"]), claude_cost(s), "claude"))
    return out


def _parse(stamp):
    d = datetime.datetime.fromisoformat(stamp)
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def week_start(now):
    local = now.astimezone(PACIFIC)
    monday = (local - datetime.timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday


def days_left(now):
    """Days remaining in the Pacific Monday-Sunday week, today included (7 on Monday, 1 on Sunday)."""
    return 7 - now.astimezone(PACIFIC).weekday()


def per_venue_cost(events, menu_entries, now):
    """Average dollars per imported venue over the last HISTORY_DAYS, or the default when there are too few imports."""
    since = now - datetime.timedelta(days=HISTORY_DAYS)
    venues = sum(1 for rec in menu_entries if rec.get("t") and _parse(rec["t"]) >= since)
    spent = sum(c for t, c, _ in events if t >= since)
    if venues < MIN_HISTORY_VENUES or spent <= 0:
        return _float_env("PER_VENUE_USD_DEFAULT", 0.10), "default"
    return spent / venues, f"measured over {venues} venues in {HISTORY_DAYS} days"


def plan(events, menu_entries, places_left, now=None, weekly_budget=None):
    """The gate's numbers. Pure: it takes the logs and returns a dict, so it can be tested without the network."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    weekly = _float_env("WEEKLY_BUDGET_USD", 0) if weekly_budget is None else weekly_budget
    start = week_start(now)
    local_today = now.astimezone(PACIFIC).date()
    spent_week = sum(c for t, c, _ in events if t >= start) + _float_env("BUDGET_ALREADY_SPENT_USD", 0)
    spent_today = sum(c for t, c, _ in events if t.astimezone(PACIFIC).date() == local_today)
    spent_before_today = spent_week - spent_today
    n = days_left(now)
    today_allowance = max((weekly - spent_before_today) / n, 0)
    today_left = max(today_allowance - spent_today, 0)
    safety = min(max(_float_env("BUDGET_SAFETY", 0.8), 0), 1)
    run_budget = today_left * safety
    per_venue, basis = per_venue_cost(events, menu_entries, now)
    venues = int(run_budget // per_venue) if per_venue > 0 else 0
    max_cells = int(_float_env("MAX_CELLS", 5))
    cells = min(venues // CELL_VENUES, max_cells)
    by_source = defaultdict(float)
    for t, c, src in events:
        if t >= start:
            by_source[src] += c
    reason = ""
    if weekly <= 0:
        reason = "WEEKLY_BUDGET_USD is not set"
    elif spent_week >= weekly:
        reason = "the week's budget is used"
    elif places_left <= 0:
        reason = "today's Google Places requests are used"
    elif cells < 1:
        reason = "today's remaining allowance does not cover a cell"
    return {
        "weekly_budget_usd": round(weekly, 2), "spent_week_usd": round(spent_week, 2), "spent_today_usd": round(spent_today, 2),
        "week_by_source_usd": {k: round(v, 2) for k, v in sorted(by_source.items())},
        "days_left": n, "today_allowance_usd": round(today_allowance, 2), "today_left_usd": round(today_left, 2),
        "run_budget_usd": round(run_budget, 2), "per_venue_usd": round(per_venue, 3), "per_venue_basis": basis,
        "allowed_venues": venues, "max_cells": cells, "places_left_today": places_left,
        "proceed": reason == "", "reason": reason,
    }


# ---------------------------------------------------------------- reading the branches

def _branch_readable(branch):
    """True when origin answers about the branch (it may not exist yet); False when origin cannot be reached."""
    out = gemini_usage.git("ls-remote", "--heads", "origin", branch)
    return out.returncode == 0


def load_inputs():
    """(gemini entries, menu-log entries, places requests left today), or None when a log branch cannot be reached."""
    if not (_branch_readable(gemini_usage.BRANCH) and _branch_readable(menu_log.BRANCH) and _branch_readable(places_usage.BRANCH)):
        return None
    return gemini_usage.load_entries(), menu_log.load_entries(), places_usage.remaining_today()


def blocked_plan(reason):
    return {"proceed": False, "reason": reason, "run_budget_usd": 0, "allowed_venues": 0, "max_cells": 0}


# ---------------------------------------------------------------- output

def gemini_report(entries, recent=15, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start, today = week_start(now), now.astimezone(PACIFIC).date()
    rows = [(_parse(e["t"]), e) for e in entries if e.get("t")]
    week = [(t, e) for t, e in rows if t >= start]
    day = [(t, e) for t, e in week if t.astimezone(PACIFIC).date() == today]

    def line(label, group):
        ok = [e for _, e in group if e.get("ok", True)]
        cost = sum(gemini_usage.entry_cost(e) for e in ok)
        return (f"{label}: {len(ok)} calls, {sum(e.get('in', 0) for e in ok):,} in / {sum(e.get('out', 0) for e in ok):,} out / "
                f"{sum(e.get('think', 0) for e in ok):,} thinking tokens, {sum(e.get('searches', 0) for e in ok)} searches, about ${cost:.2f}")
    out = [f"Gemini usage, {now.astimezone(PACIFIC):%A %B %-d, %Y} (Pacific time; the week starts Monday)", "", line("Today", day), line("This week", week), ""]
    failed = sum(1 for _, e in week if not e.get("ok", True))
    if failed:
        out += [f"{failed} call(s) this week failed and are not counted.", ""]
    out.append(f"Recent calls (newest first, {recent} shown)")
    for t, e in sorted(rows, key=lambda r: r[0], reverse=True)[:recent]:
        status = f"{e.get('in', 0):,} in / {e.get('out', 0) + e.get('think', 0):,} out, {e.get('searches', 0)} searches" if e.get("ok", True) else "failed, not counted"
        source = f"{e.get('workflow')} run {e['run']}" if e.get("run") else e.get("workflow", "")
        out.append(f"  {t.astimezone(PACIFIC):%b %-d %-I:%M %p}  {e.get('model')}  {status}  [{source}]")
    if not rows:
        out.append("  (nothing logged yet)")
    return "\n".join(out)


def format_plan(p):
    if "weekly_budget_usd" not in p:
        return f"Budget gate: stop. {p['reason']}."
    lines = [
        f"Weekly budget ${p['weekly_budget_usd']:.2f}: ${p['spent_week_usd']:.2f} spent "
        f"({', '.join(f'{k} ${v:.2f}' for k, v in p['week_by_source_usd'].items()) or 'nothing logged yet'}), {p['days_left']} day(s) left including today.",
        f"Today's pace: ${p['today_allowance_usd']:.2f} allowed, ${p['spent_today_usd']:.2f} spent, ${p['today_left_usd']:.2f} left; this run may use ${p['run_budget_usd']:.2f}.",
        f"One venue costs about ${p['per_venue_usd']:.3f} ({p['per_venue_basis']}), so up to {p['allowed_venues']} venues, {p['max_cells']} cell(s).",
        f"Google Places requests left today: {p['places_left_today']}.",
        "Budget gate: go." if p["proceed"] else f"Budget gate: stop. {p['reason']}.",
    ]
    return "\n".join(lines)


def write_outputs(p):
    flat = {"proceed": str(p["proceed"]).lower(), "reason": p["reason"], "run_budget_usd": p["run_budget_usd"],
            "allowed_venues": p["allowed_venues"], "max_cells": p["max_cells"]}
    for k, v in flat.items():
        print(f"{k}={v}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in flat.items():
                f.write(f"{k}={v}\n")


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command not in ("report", "gate"):
        sys.exit("usage: budget.py [report | gate]")
    inputs = load_inputs()
    p = blocked_plan("a usage log could not be read from origin, so spend cannot be measured") if inputs is None else \
        plan(spend_events(inputs[0], inputs[1]), inputs[1], inputs[2])
    if command == "report":
        print(format_plan(p))
    else:
        print(format_plan(p), file=sys.stderr)
        write_outputs(p)


if __name__ == "__main__":
    main()
