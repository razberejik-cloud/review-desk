"""
Step 6b - Urgent escalations (hourly, rules only)
=================================================

Some reviews can't wait for the morning report: a child in danger, a legal
threat, or a report that the app breaks a Google Play policy (gambling or
adult ads in a kids game, ads that install apps, kids' data). These can get
an app suspended from the store, so the product manager of that app and Legal
must hear about them within the hour.

How it runs:
  * Every hour (not daily): pull the newest reviews and run ONLY the keyword
    rules from classify.py. No model, takes seconds, can't be "unsure".
  * Each hit becomes or joins an alert for (app, type). Reports on the same
    problem within 72 hours join the open alert instead of paging people again.
  * The alert pages the owners for that app and type, with a deadline (SLA).
    3+ reports in 24h escalates to the Head of Product.
  * These reviews never get an automatic reply. A person drafts the answer and
    Legal approves it before anything is posted publicly.

Output (in reviews.db):
  urgent_alerts         one row per alert: type, app, first/last report, detected_at,
                        paged roles, SLA, policy at risk, actions, message
  urgent_notifications  one row per person paged

Run after triage.py:  python urgent.py
"""

import json
import sqlite3
from datetime import datetime, timedelta

DB = "reviews.db"
CHECK_EVERY = timedelta(hours=1)
MERGE_WINDOW = timedelta(hours=72)
DASHBOARD = "Review Morning Desk"

# who owns what (fake contacts for the POC - in production this lives in a config sheet)
OWNERS = {
    "pm": {"Merge Meadow": "pm.mergemeadow@example.com", "Smash Runner": "pm.smashrunner@example.com",
           "Tile Twist": "pm.tiletwist@example.com", "PeleGame": "pm.pelegame@example.com"},
    "legal": "legal-oncall@example.com",
    "trust_safety": "trust-safety@example.com",
    "dpo": "privacy@example.com",
    "ad_ops": "ad-monetization@example.com",
    "support_lead": "support-lead@example.com",
    "head_of_product": "head-of-product@example.com",
}

TYPES = {   # checked in this order; a review belongs to the first type it matches
    "CHILD_SAFETY": dict(flags={"child_safety"}, sla_h=1, label="Child safety",
                         roles=["pm", "legal", "trust_safety"],
                         policy="Google Play child safety standards and Families policy",
                         actions=["Trust & Safety reviews the reported account and chat logs now",
                                  "Preserve evidence; Legal decides on reporting to authorities",
                                  "Personal reply drafted by Trust & Safety, approved by Legal"]),
    "STORE_POLICY": dict(flags={"store_policy"}, sla_h=4, label="Store policy risk",
                         roles=["pm", "legal", "ad_ops"],
                         policy="Google Play Ads policy and Families policy (ads in apps used by children)",
                         actions=["Ad ops checks which ad network served it and blocks that category/network",
                                  "PM decides whether to ship a hotfix or pause the network remotely",
                                  "No public reply until Legal approves the wording"]),
    "LEGAL": dict(flags={"legal"}, sla_h=4, label="Legal threat",
                  roles=["legal", "pm", "support_lead"],
                  policy="Consumer protection and payments (refund and billing obligations)",
                  actions=["Support lead checks the player's orders today",
                           "Legal approves any reply that mentions money or responsibility"]),
    "PRIVACY": dict(flags={"privacy"}, sla_h=24, label="Privacy / kids' data",
                    roles=["dpo", "legal", "pm"],
                    policy="Google Play User Data policy and the Data safety section",
                    actions=["Privacy team checks which permissions this version requests and why",
                             "Confirm the Data safety form matches what the app really collects"]),
}


def detected_at(ts):
    """The hourly job sees a review at the next full hour, and pages 5 minutes later."""
    nxt = (ts + CHECK_EVERY).replace(minute=0, second=0, microsecond=0)
    return nxt + timedelta(minutes=5)


def run():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("""
        SELECT r.review_id, a.app_name AS app, r.text, r.star_rating, r.reviewer_language AS lang,
               r.app_version_name AS version, r.thumbs_up, r.last_modified_at AS ts, ra.flags
        FROM reviews r JOIN apps a USING(package_name) JOIN review_analysis ra USING(review_id)
        WHERE ra.flags <> '' ORDER BY r.last_modified_at""")]

    alerts, open_by_key = [], {}
    for r in rows:
        flags = set(r["flags"].split(","))
        typ = next((t for t, cfg in TYPES.items() if cfg["flags"] & flags), None)
        if not typ:
            continue
        ts = datetime.fromisoformat(r["ts"])
        det = detected_at(ts)
        key = (r["app"], typ)
        a = open_by_key.get(key)
        if a and ts - a["last_report"] <= MERGE_WINDOW:
            a["reviews"].append(r)
            a["last_report"] = ts
            recent = [x for x in a["reviews"] if ts - datetime.fromisoformat(x["ts"]) <= timedelta(hours=24)]
            if len(recent) >= 3 and not a["escalated_at"]:
                a["escalated_at"] = det
            continue
        cfg = TYPES[typ]
        a = dict(id=f"U{len(alerts) + 1:03d}", type=typ, app=r["app"], first_report=ts, last_report=ts,
                 detected_at=det, escalated_at=None, reviews=[r], cfg=cfg)
        alerts.append(a)
        open_by_key[key] = a

    con.execute("DROP TABLE IF EXISTS urgent_alerts")
    con.execute("""CREATE TABLE urgent_alerts (alert_id TEXT PRIMARY KEY, type TEXT, label TEXT, app TEXT,
                   first_report TEXT, last_report TEXT, detected_at TEXT, minutes_to_page INTEGER, escalated_at TEXT,
                   reports INTEGER, sla_hours INTEGER, due_by TEXT, paged TEXT, policy TEXT, actions TEXT,
                   review_ids TEXT, message TEXT)""")
    con.execute("DROP TABLE IF EXISTS urgent_notifications")
    con.execute("CREATE TABLE urgent_notifications (alert_id TEXT, role TEXT, address TEXT, sent_at TEXT, reason TEXT)")

    for a in alerts:
        cfg, first = a["cfg"], a["reviews"][0]
        paged = []
        for role in cfg["roles"]:
            addr = OWNERS[role][a["app"]] if role == "pm" else OWNERS[role]
            paged.append((role, addr, a["detected_at"].isoformat(), "new alert"))
        if a["escalated_at"]:
            paged.append(("head_of_product", OWNERS["head_of_product"], a["escalated_at"].isoformat(), "3+ reports in 24h"))
        due = a["detected_at"] + timedelta(hours=cfg["sla_h"])
        mins = int((a["detected_at"] - a["first_report"]).total_seconds() // 60)
        msg = (f"URGENT · {cfg['label']} · {a['app']}\n"
               f"{len(a['reviews'])} report(s) since {a['first_report']:%d %b %H:%M} UTC. Respond by {due:%d %b %H:%M} UTC ({cfg['sla_h']}h).\n"
               f"At risk: {cfg['policy']}.\n"
               f"Latest review ({first['star_rating']}★, {first['lang']}, v{first['version']}): \"{a['reviews'][-1]['text'][:200]}\"\n"
               f"Next steps: " + "; ".join(cfg["actions"]) + ".\n"
               f"Automatic replies are blocked for these reviews. Open {DASHBOARD} for all reports.")
        con.execute("INSERT INTO urgent_alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a["id"], a["type"], cfg["label"], a["app"], a["first_report"].isoformat(), a["last_report"].isoformat(),
                     a["detected_at"].isoformat(), mins, a["escalated_at"].isoformat() if a["escalated_at"] else None,
                     len(a["reviews"]), cfg["sla_h"], due.isoformat(), json.dumps([p[:2] for p in paged]), cfg["policy"],
                     json.dumps(cfg["actions"]), json.dumps([x["review_id"] for x in a["reviews"]]), msg))
        con.executemany("INSERT INTO urgent_notifications VALUES (?,?,?,?,?)", [(a["id"], *p) for p in paged])
    con.commit()

    print(f"{len(alerts)} urgent alerts from {sum(len(a['reviews']) for a in alerts)} reports\n")
    for a in alerts:
        mins = int((a["detected_at"] - a["first_report"]).total_seconds() // 60)
        print(f"  {a['id']} {a['cfg']['label']:20s} {a['app']:13s} reports={len(a['reviews']):<3} "
              f"first {a['first_report']:%d %b %H:%M} -> paged {a['detected_at']:%H:%M} ({mins} min)"
              f"{'  ESCALATED to Head of Product' if a['escalated_at'] else ''}")
        print(f"       paged: {', '.join(a['cfg']['roles'])}   SLA {a['cfg']['sla_h']}h")
    worst = max(int((a["detected_at"] - a["first_report"]).total_seconds() // 60) for a in alerts)
    print(f"\nSlowest time from review to page: {worst} minutes")
    con.close()


if __name__ == "__main__":
    run()
