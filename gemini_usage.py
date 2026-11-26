"""Count the Gemini tokens this repository uses, and stop a run that passes its own spending limit.

Every Gemini call made through the importer's client (add_venue_from_issue.py, batch_update_venues.py, update_venues.py)
is logged with record(): the model, input / output / thinking tokens, and how many Google Search grounding queries the
call ran. The workflows run on throwaway runners, so each ends with `python gemini_usage.py publish`, which appends the
run's log to usage.jsonl on the gemini-usage branch (kept off main so it never triggers a site rebuild). budget.py reads
it back to price the week's spend.

    python gemini_usage.py report [--recent N]     tokens and estimated dollars, today and this week
    python gemini_usage.py publish                 save this run's log (workflows run this last)

One line per call:
    {"v": 1, "t": ISO time, "run": run id, "workflow": name, "model": "gemini-3.6-flash",
     "in": prompt tokens, "out": output tokens, "think": thinking tokens, "searches": grounding queries, "ok": true}
Calls that raised an error are logged with ok=false and no tokens (Google does not bill a refused request).

Dollar figures are estimates from the rates in budget.RATES. They exist to pace spending; the spend cap in Google AI
Studio is still the record of what was billed.

RUN_BUDGET_USD (optional): once the run's estimated Gemini spend (every process that shares the log file) passes it, check_run_budget() raises
GeminiRunBudgetReached and the importer's client stops calling Gemini, the same way it does for the monthly spending cap."""
import os
import sys
import json
import tempfile
import subprocess
import datetime

BRANCH = "gemini-usage"
DATA_FILE = "usage.jsonl"
VERSION = 1


class GeminiRunBudgetReached(Exception):
    """This run has used the Gemini allowance it was given (RUN_BUDGET_USD)."""


_run_cost = 0.0


def log_path():
    return os.environ.get("GEMINI_USAGE_LOG") or os.path.join(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "gemini-usage.jsonl")


def _num(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def search_queries_in(response):
    """How many Google Search queries a response shows. Grounding metadata lists them when the answer cites pages. A call
    made with tool_config include_server_side_tool_invocations also carries them as GOOGLE_SEARCH tool_call parts, and
    those appear even when the answer has no grounding metadata (a JSON-only reply), so the larger of the two counts is used
    (they describe the same queries when both are present). Never raises."""
    try:
        grounded = called = 0
        for candidate in getattr(response, "candidates", None) or []:
            queries = getattr(getattr(candidate, "grounding_metadata", None), "web_search_queries", None)
            grounded += len(queries) if isinstance(queries, (list, tuple)) else 0
            for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
                call = getattr(part, "tool_call", None)
                kind = getattr(getattr(call, "tool_type", None), "name", "") if call else ""
                if "GOOGLE_SEARCH" in str(kind):
                    args = getattr(call, "args", None)
                    queries = args.get("queries") if isinstance(args, dict) else None
                    called += len(queries) if isinstance(queries, (list, tuple)) and queries else 1
        return max(grounded, called)
    except Exception:
        return 0


def usage_from_response(response):
    """(prompt tokens, output tokens, thinking tokens, grounding queries) from a generate_content response. Missing
    fields count as 0, so an unexpected response shape never raises."""
    meta = getattr(response, "usage_metadata", None)
    tokens_in = _num(getattr(meta, "prompt_token_count", 0)) + _num(getattr(meta, "tool_use_prompt_token_count", 0))
    tokens_out = _num(getattr(meta, "candidates_token_count", 0))
    think = _num(getattr(meta, "thoughts_token_count", 0))
    searches = search_queries_in(response)
    return tokens_in, tokens_out, think, searches


def entry_cost(entry, rates=None):
    """Estimated dollars for one logged call."""
    import budget
    r = (rates or budget.RATES)["gemini"]
    model_rates = r.get(entry.get("model")) or r["default"]
    return (entry.get("in", 0) * model_rates["in"] + (entry.get("out", 0) + entry.get("think", 0)) * model_rates["out"]) / 1e6 \
        + entry.get("searches", 0) * r["search_each"]


def record(model, response=None, ok=True):
    """Append one call to the run's log and add its cost to the run total. Never raises: a logging problem must not stop
    a venue from being added."""
    global _run_cost
    try:
        tokens_in, tokens_out, think, searches = usage_from_response(response) if ok else (0, 0, 0, 0)
        entry = {"v": VERSION, "t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                 "run": os.environ.get("GITHUB_RUN_ID", ""), "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
                 "model": str(model or ""), "in": tokens_in, "out": tokens_out, "think": think, "searches": searches, "ok": bool(ok)}
        with open(log_path(), "a") as f:
            f.write(json.dumps(entry) + "\n")
        _run_cost += entry_cost(entry)
    except Exception as e:
        print(f"Could not log the Gemini call: {e}")


def run_cost():
    """Estimated Gemini dollars spent by this run. add_city.py starts the importer as a separate process for every venue,
    so the run's total is read from the log file they all append to; this process's own count covers a log that cannot
    be read."""
    total = 0.0
    try:
        with open(log_path()) as f:
            for line in f:
                try:
                    total += entry_cost(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return max(total, _run_cost)


def run_limit():
    try:
        return float(os.environ.get("RUN_BUDGET_USD", "") or 0) or None
    except ValueError:
        return None


def run_budget_spent():
    """True when RUN_BUDGET_USD is set and this run has spent it."""
    limit = run_limit()
    return limit is not None and run_cost() >= limit


def check_run_budget():
    """Raise GeminiRunBudgetReached when RUN_BUDGET_USD is set and this run has spent it."""
    if run_budget_spent():
        raise GeminiRunBudgetReached(f"this run has used its Gemini allowance (about ${run_cost():.2f} of ${run_limit():.2f})")


class _TrackedModels:
    def __init__(self, models):
        self._models = models

    def __getattr__(self, name):
        # list(), get() and the rest are not generate_content calls; they go straight to the client
        return getattr(self._models, name)

    def generate_content(self, *args, **kwargs):
        check_run_budget()
        try:
            response = self._models.generate_content(*args, **kwargs)
        except Exception:
            record(kwargs.get("model"), ok=False)
            raise
        record(kwargs.get("model"), response)
        return response


class TrackedClient:
    """A genai client whose generate_content calls are logged and held to RUN_BUDGET_USD. For scripts that do not use the
    importer's guarded client."""
    def __init__(self, client):
        self._client = client
        self.models = _TrackedModels(getattr(client, "models", None))

    def __getattr__(self, name):
        return getattr(self._client, name)


# ---------------------------------------------------------------- the log on its branch

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def publish():
    """Append this run's log to usage.jsonl on the gemini-usage branch (created on first use)."""
    path = log_path()
    if not os.path.exists(path) or not os.path.getsize(path):
        print("No Gemini calls to log.")
        return
    with open(path) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    ident = ["-c", "user.name=github-actions[bot]", "-c", "user.email=github-actions[bot]@users.noreply.github.com"]
    work = os.path.join(tempfile.mkdtemp(), "usage")
    for attempt in range(1, 7):
        if git("worktree", "prune").returncode:
            break
        have_branch = git("fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}").returncode == 0
        if git("worktree", "add", "--detach", work, f"origin/{BRANCH}" if have_branch else "HEAD").returncode:
            break
        if not have_branch:
            git("checkout", "--orphan", BRANCH, cwd=work)
            git("rm", "-rf", "--quiet", ".", cwd=work)
        with open(os.path.join(work, DATA_FILE), "a") as f:
            f.write("\n".join(lines) + "\n")
        git("add", DATA_FILE, cwd=work)
        message = f"Log {len(lines)} Gemini call(s) from run {os.environ.get('GITHUB_RUN_ID', 'local')}"
        if git(*ident, "commit", "--quiet", "-m", message, cwd=work).returncode == 0:
            pushed = git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=work)
            if pushed.returncode == 0:
                os.remove(path)
                git("worktree", "remove", "--force", work)
                print(f"Logged {len(lines)} Gemini call(s) to the {BRANCH} branch.")
                return
            print(f"Push rejected (attempt {attempt}): {pushed.stderr.strip()[-200:]}")
        git("worktree", "remove", "--force", work)
    print("Could not save the Gemini log; the calls from this run are not counted.")


def load_entries():
    """Every logged call, read from the gemini-usage branch on origin. [] when the branch does not exist or cannot be read."""
    if git("fetch", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}").returncode:
        return []
    entries = []
    for line in git("show", f"origin/{BRANCH}:{DATA_FILE}").stdout.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "publish":
        publish()
    elif command == "report":
        import budget
        recent = int(sys.argv[sys.argv.index("--recent") + 1]) if "--recent" in sys.argv else 15
        print(budget.gemini_report(load_entries(), recent))
    else:
        sys.exit("usage: gemini_usage.py [report [--recent N] | publish]")


if __name__ == "__main__":
    main()
