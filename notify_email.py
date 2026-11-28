"""Email a short summary of a Daily Grid Search run.

Usage: python notify_email.py          (the last step of daily-search.yml, run with `if: always()`)

It reads the run's facts from environment variables the workflow sets (JOB_STATUS, DRY_RUN, GATE_REASON, PICK_REASON,
CELLS, ADDED, APPROVED, DENIED, HELD, TRIAGE_MODE, PR_URL, ISSUE_NUMBER, RUN_URL, BUDGET_LINE) and the triage summary file
(`triage-summary.md` in RUNNER_TEMP) when there is one.

Mail goes out over SMTP with the standard library. Settings (repository secrets, Settings > Secrets and variables > Actions):
  EMAIL_TO         where the summary goes (required; without it nothing is sent)
  SMTP_HOST        for example smtp.gmail.com (required)
  SMTP_PORT        587 (STARTTLS, the default) or 465 (SSL)
  SMTP_USER        the login, also the sender unless EMAIL_FROM is set
  SMTP_PASSWORD    the password or app password
  EMAIL_FROM       optional sender address
Never raises and never fails the run: a missing setting or a send error is printed and the script exits 0."""
import os
import smtplib
import ssl
from email.message import EmailMessage

MAX_TABLE_CHARS = 4000


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def build():
    """Return (subject, body) for this run."""
    status = env("JOB_STATUS", "success")
    dry = env("DRY_RUN") == "true"
    cells = env("CELLS")
    gate, pick = env("GATE_REASON"), env("PICK_REASON")
    issue, pr, run_url = env("ISSUE_NUMBER"), env("PR_URL"), env("RUN_URL")
    lines = []

    if status != "success":
        headline = "failed"
        lines.append("The run hit an error. Open the run log for details.")
    elif dry:
        headline = "dry run"
        lines.append("Dry run: nothing was searched or opened.")
    elif gate:
        headline = "skipped (budget)"
        lines.append(f"The budget gate stopped the run: {gate}")
    elif pick or not cells:
        headline = "nothing to search"
        lines.append(f"No cells were searched. {pick}".strip())
    else:
        added = env("ADDED", "0")
        headline = f"{added} venue{'' if added == '1' else 's'} found"
        lines.append(f"Cells searched: {cells}")
        lines.append(f"Venues found: {added}")
        if env("APPROVED") or env("DENIED") or env("HELD"):
            mode = env("TRIAGE_MODE", "suggest")
            lines.append(f"Triage ({mode}): {env('APPROVED', '0')} approve, {env('DENIED', '0')} deny, {env('HELD', '0')} hold")
        if env("TRIAGE_MODE") == "apply":
            lines.append("Approved venues were added to the map by the run itself.")
        elif pr:
            lines.append(f"Pull request to review: {pr}")
        elif issue:
            lines.append(f"Issue: {run_url.rsplit('/actions/', 1)[0]}/issues/{issue}" if run_url else f"Issue #{issue}")

    if env("BUDGET_LINE"):
        lines.append(f"Budget: {env('BUDGET_LINE')}")
    if run_url:
        lines.append(f"Run: {run_url}")

    table = ""
    path = os.path.join(env("RUNNER_TEMP", "/tmp"), "triage-summary.md")
    if status == "success" and not dry and os.path.exists(path):
        with open(path) as f:
            table = f.read().strip()
        if len(table) > MAX_TABLE_CHARS:
            table = table[:MAX_TABLE_CHARS].rsplit("\n", 1)[0] + "\n[trimmed; see the run for the rest]"
    body = "\n".join(lines) + (f"\n\n{table}" if table else "") + "\n"
    return f"Date Nite daily grid search: {headline}", body


def send(subject, body):
    to, host = env("EMAIL_TO"), env("SMTP_HOST")
    if not to or not host:
        print("Email not sent: set the EMAIL_TO and SMTP_HOST secrets (and SMTP_USER, SMTP_PASSWORD) to turn it on.")
        return
    user, password, port = env("SMTP_USER"), env("SMTP_PASSWORD"), int(env("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = env("EMAIL_FROM") or user or to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=ctx)
    with server:
        if user and password:
            server.login(user, password)
        server.send_message(msg)
    print(f"Emailed the summary to {to}.")


def main():
    try:
        subject, body = build()
        print(subject + "\n" + body)
        send(subject, body)
    except Exception as e:  # never fail the run over an email
        print(f"Could not send the summary email: {e}")


if __name__ == "__main__":
    main()
