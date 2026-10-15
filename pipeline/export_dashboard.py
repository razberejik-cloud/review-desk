"""
Step 7 - Export everything the dashboard needs from reviews.db into one JSON file.
In production this runs right after triage.py every morning, and the dashboard
reads the fresh file. Run:  python export_dashboard.py  -> dashboard_data.json
"""
import json
import random
import sqlite3
from collections import Counter, defaultdict

con = sqlite3.connect("reviews.db")
con.row_factory = sqlite3.Row
q = lambda sql, *a: [dict(r) for r in con.execute(sql, a)]

rows = q("""SELECT r.review_id, a.app_name AS app, substr(r.last_modified_at,1,10) AS day, r.star_rating AS stars,
                   r.first_star_rating AS first_stars, r.reviewer_language AS lang, r.app_version_name AS version,
                   r.device_name AS device, r.text, r.developer_reply AS reply, ra.category, ra.is_noise, ra.noise_reason,
                   t.lane, t.reason, t.priority, t.team, t.sla_hours, t.draft, t.reply_status
            FROM reviews r JOIN apps a USING(package_name) JOIN review_analysis ra USING(review_id)
            JOIN triage t USING(review_id) ORDER BY r.last_modified_at""")
apps = [r["app_name"] for r in q("SELECT app_name FROM apps")]
days = sorted({r["day"] for r in rows})

# per day per app: totals, lanes, categories (non-noise)
daily = defaultdict(lambda: {"total": 0, "lanes": Counter(), "cats": Counter(), "langs": Counter()})
for r in rows:
    d = daily[(r["day"], r["app"])]
    d["total"] += 1
    d["lanes"][r["lane"]] += 1
    d["langs"][r["lang"]] += 1
    if not r["is_noise"] and r["category"]:
        d["cats"][r["category"]] += 1
daily_out = {f"{k[0]}|{k[1]}": {"t": v["total"], "l": dict(v["lanes"]), "c": dict(v["cats"])} for k, v in daily.items()}

# collector log (what the 06:00 job saw)
runs = defaultdict(lambda: {"fetched": 0, "new": 0, "edited": 0, "repeats": 0})
for r in q("SELECT substr(run_at,1,10) AS d, fetched, new_reviews, edited, repeats FROM ingestion_runs"):
    x = runs[r["d"]]
    x["fetched"] += r["fetched"]; x["new"] += r["new_reviews"]; x["edited"] += r["edited"]; x["repeats"] += r["repeats"]

# releases: first day each version shows up
releases = defaultdict(dict)
for r in rows:
    if r["version"] and r["version"] not in releases[r["app"]]:
        releases[r["app"]][r["version"]] = r["day"]

# issues + their signals + themes
alerts = q("SELECT alert_key, type, app, category, first_raised, evidence FROM alerts")
themes = {t["theme_id"]: t for t in q("SELECT theme_id, genre, category, keywords, quote, born_on, size, languages FROM themes")}
issues = []
for i in q("SELECT * FROM issues"):
    sigs = [dict(type=a["type"], day=a["first_raised"], ev=json.loads(a["evidence"])) for a in alerts
            if a["app"] == i["app"] and a["category"] == i["category"]]
    for s in sigs:
        s["ev"].pop("review_ids", None)
    th = [dict(id=t, **{k: themes[t][k] for k in ("keywords", "quote", "born_on", "size")},
               langs=json.loads(themes[t]["languages"])) for t in json.loads(i["themes"]) if t in themes]
    ev = json.loads(i["evidence"])
    ev.pop("review_ids", None)
    issues.append(dict(app=i["app"], category=i["category"], lead=i["lead_signal"], first=i["first_raised"],
                       priority=i["priority"], signals=sorted(sigs, key=lambda s: s["day"]), themes=th, ev=ev))

# human queue (all), auto-reply samples (a few per day)
queue = [dict(id=r["review_id"], day=r["day"], app=r["app"], stars=r["stars"], first=r["first_stars"], lang=r["lang"],
              version=r["version"], text=r["text"], lane=r["lane"], reason=r["reason"], p=r["priority"], team=r["team"],
              sla=r["sla_hours"], draft=r["draft"]) for r in rows if r["lane"] in ("SAFETY", "LEGAL", "HUMAN_VIP", "HUMAN_CHECK")]
rng = random.Random(3)
by_day = defaultdict(list)
for r in rows:
    if r["reply_status"] and r["reply_status"].startswith("posted"):
        by_day[r["day"]].append(r)
samples = []
for d, lst in by_day.items():
    inc = [r for r in lst if r["lane"] == "INCIDENT"]
    pick = rng.sample(inc, min(2, len(inc))) + rng.sample([r for r in lst if r["lane"] == "AUTO"], min(4, len(lst)))
    for r in pick:
        samples.append(dict(day=d, app=r["app"], stars=r["stars"], lang=r["lang"], cat=r["category"], text=r["text"],
                            reply=r["reply"], lane=r["lane"], status=r["reply_status"]))

# events the run log should mention
import re
from triage import T
events = defaultdict(list)
edits = Counter()
for r in rows:
    m = re.search(r"edited (\d{4}-\d\d-\d\d) \(fixed in ([\d.]+)\)", r["reply_status"] or "")
    if m:
        edits[(m.group(1), r["app"], r["version"], m.group(2))] += 1
for (d, app, v, nv), n in edits.items():
    events[d].append(f"{app} v{nv} fixed the v{v} incident: {n} earlier replies edited to say the fix is out")
for s_ in samples:
    m = re.search(r"edited (\d{4}-\d\d-\d\d)", s_["status"] or "")
    if m and s_["lane"] == "INCIDENT":
        lang = s_["lang"] if s_["lang"] in T["incident"] else "en"
        ver = next((r["version"] for r in rows if r["text"] == s_["text"]), "")
        s_["reply_before"] = T["incident"][lang].format(version=ver)
        s_["edited_on"] = m.group(1)

# urgent alerts (hourly channel)
texts = {r["review_id"]: r for r in rows}
urgent = []
for u in q("SELECT * FROM urgent_alerts ORDER BY detected_at"):
    revs = [dict(ts=t["ts"], text=texts[i]["text"], stars=texts[i]["stars"], lang=texts[i]["lang"], version=texts[i]["version"])
            for i in json.loads(u["review_ids"]) for t in [dict(ts=con.execute("SELECT last_modified_at FROM reviews WHERE review_id=?", (i,)).fetchone()[0])]]
    urgent.append(dict(id=u["alert_id"], type=u["type"], label=u["label"], app=u["app"], first=u["first_report"],
                       detected=u["detected_at"], mins=u["minutes_to_page"], escalated=u["escalated_at"], sla=u["sla_hours"],
                       due=u["due_by"], paged=json.loads(u["paged"]), policy=u["policy"], actions=json.loads(u["actions"]),
                       message=u["message"], reviews=revs))

data = dict(urgent=urgent, events=events, apps=apps, days=days, daily=daily_out, runs=runs, releases=releases, issues=issues, queue=queue,
            samples=samples, totals=dict(reviews=len(rows), languages=len({r["lang"] for r in rows})))
json.dump(data, open("dashboard_data.json", "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
print(f"days {len(days)}, issues {len(issues)}, queue {len(queue)}, samples {len(samples)}")
import os; print(f"{os.path.getsize('dashboard_data.json') / 1024:.0f} KB")
