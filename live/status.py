"""
Alert follow-up: acknowledge, resolve, reopen, and escalate when nobody answers.

  python live/status.py comment <issue_number> <github_user> <comment_file>
      handles /ack, /resolve <what was done>, /reopen written on the alert's issue
  python live/status.py sla
      run every 15 minutes: an open alert past its deadline with no /ack escalates
      to the Head of Product (once), then again to the on-call director after 2x the SLA

Every change is an event in the alert's history (site/live.json) and in the
alert_events table of data/reviews.db. Writes live/out/<issue>.md for each issue
that needs a comment, and live/out/actions.txt with label/close actions.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(ROOT, "site", "live.json")
DB = os.path.join(ROOT, "data", "reviews.db")
OUT = os.path.join(ROOT, "live", "out")
OWNERS = json.load(open(os.path.join(ROOT, "config", "owners.json"), encoding="utf-8"))["contacts"]
now = datetime.now(timezone.utc).replace(microsecond=0)


def load():
    return json.load(open(LIVE, encoding="utf-8")) if os.path.exists(LIVE) else []


def save(live):
    json.dump(live, open(LIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def log_db(issue, event, by, note):
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS alert_events (issue INTEGER, review_id TEXT, at TEXT, event TEXT, by TEXT, note TEXT)")
    con.execute("INSERT INTO alert_events VALUES (?,?,?,?,?,?)", (issue, f"live-{issue}", now.isoformat(), event, by, note))
    con.commit(); con.close()


def write(issue, text, action=None):
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, f"{issue}.md"), "w", encoding="utf-8").write(text)
    if action:
        open(os.path.join(OUT, "actions.txt"), "a").write(f"{issue} {action}\n")


def mins(a, b):
    m = int((b - a).total_seconds() // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def comment(issue, user, body_file):
    live = load()
    entry = next((x for x in live if x.get("issue") == int(issue)), None)
    if not entry or not entry.get("urgent"):
        return
    st = entry["urgent"]["status"]
    text = open(body_file, encoding="utf-8").read().strip()
    cmd, _, note = text.partition(" ")
    cmd = cmd.lower()
    received = datetime.fromisoformat(entry["received_at"])
    if cmd == "/ack" and st["state"] in ("open", "overdue"):
        st["state"] = "acknowledged"
        st["events"].append(dict(at=now.isoformat(), event="acknowledged", by=user, note=note.strip()))
        write(issue, f"✅ Acknowledged by @{user} {mins(received, now)} after the page. Escalation clock stopped.")
    elif cmd == "/resolve" and st["state"] != "resolved":
        st["state"] = "resolved"
        st["events"].append(dict(at=now.isoformat(), event="resolved", by=user, note=note.strip() or "resolved"))
        write(issue, f"🟢 Resolved by @{user} {mins(received, now)} after the page: {note.strip() or '(no note)'}", "resolve")
    elif cmd == "/reopen" and st["state"] == "resolved":
        st["state"] = "acknowledged"
        st["events"].append(dict(at=now.isoformat(), event="reopened", by=user, note=note.strip()))
        write(issue, f"🔁 Reopened by @{user}.", "reopen")
    else:
        return
    log_db(int(issue), st["events"][-1]["event"], user, note.strip())
    save(live)


def sla():
    live = load()
    changed = False
    for e in live:
        u = e.get("urgent")
        if not u or u["status"]["state"] not in ("open", "overdue"):
            continue
        due = datetime.fromisoformat(u["due"])
        levels = [x["note"] for x in u["status"]["events"] if x["event"] == "escalated"]
        if now >= due and "Head of Product" not in levels:
            u["status"]["state"] = "overdue"
            u["status"]["events"].append(dict(at=now.isoformat(), event="escalated", by="system", note="Head of Product"))
            write(e["issue"], f"⏰ **Overdue.** Nobody acknowledged this {u['label'].lower()} alert within {u['sla']} h. "
                              f"Escalated to the Head of Product ({OWNERS['head_of_product']}).", "overdue")
            log_db(e["issue"], "escalated", "system", "Head of Product"); changed = True
        elif now >= due + timedelta(hours=u["sla"]) and "Director on call" not in levels:
            u["status"]["events"].append(dict(at=now.isoformat(), event="escalated", by="system", note="Director on call"))
            write(e["issue"], f"⏰ **Still not acknowledged** after {2 * u['sla']} h. Escalated to the director on call.")
            log_db(e["issue"], "escalated", "system", "Director on call"); changed = True
    if changed:
        save(live)
    print("escalations:", changed)


if __name__ == "__main__":
    if sys.argv[1] == "comment":
        comment(*sys.argv[2:5])
    else:
        sla()
