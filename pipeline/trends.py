"""
Step 5 - Trends, normalization and alerts
=========================================

Turns the classified stream into a short list of things worth acting on.

Normalization (why a small app can outrank a big one):
  share = reviews about X / all real reviews of that app (same days)
  lift  = share now / the app's own normal share
  An alert needs a high lift AND enough reviews to not be luck (binomial
  z-score + a minimum count). The absolute count is still shown, so a
  product person also sees how many players are affected.

Alert types:
  SAFETY        any child-safety / legal report -> immediately, regardless of volume
  RELEASE       a category jumps in a new app version vs the previous version
                (+ which devices it is concentrated on)
  SPIKE         a category's 7-day share vs the app's previous 4 weeks
  NEW_THEME     a theme that did not exist before was born from the stream (step 4)
  EMERGING      slow steady growth over weeks (a feature request gaining traction)
  CHRONIC       always high, no spike - a known issue for the roadmap, not an incident

The script replays the 60 days as if it ran every morning, so each alert has
the day it would FIRST have been raised. That date is the detection speed.

Output (in reviews.db):
  daily_metrics  app x category x day: count, app total, share
  alerts         one row per issue: type, app, category, first_raised, last_seen,
                 evidence (lift, z, counts, version, devices), priority

Run after cluster.py:  python trends.py
"""

import json
import math
import sqlite3
from datetime import date, timedelta

import pandas as pd

DB = "reviews.db"
RECENT = 7            # days in the "now" window
BASELINE = 28         # days in the "normal" window before it
MIN_COUNT = 15        # never alert on fewer reviews than this
MIN_LIFT = 2.0
MIN_Z = 3.0
NOT_PROBLEMS = {"praise", "other"}          # we don't alert on more praise


def z_score(k, n, p0):
    """How surprising is k out of n if the normal rate is p0 (binomial)."""
    p0 = min(max(p0, 1e-3), 0.999)
    return (k - n * p0) / math.sqrt(n * p0 * (1 - p0)) if n else 0.0


def load(con):
    df = pd.read_sql("""
        SELECT r.review_id, substr(r.last_modified_at, 1, 10) AS day, a.app_name AS app, a.genre,
               r.app_version_name AS version, r.device_name AS device, r.star_rating AS rating,
               ra.category, ra.flags, ra.is_noise, rt.theme_id
        FROM reviews r JOIN apps a USING(package_name) JOIN review_analysis ra USING(review_id)
        LEFT JOIN review_themes rt USING(review_id)""", con)
    df["day"] = pd.to_datetime(df["day"])
    return df


# ---------------------------------------------------------------------------
def detect(df, themes, as_of, history_start):
    """All alerts that would be raised on the morning of `as_of` (using data up to the day before)."""
    alerts = []
    real = df[(df.is_noise == 0) & (df.day < as_of)]
    recent = real[real.day >= as_of - pd.Timedelta(days=RECENT)]
    base = real[(real.day < as_of - pd.Timedelta(days=RECENT)) &
                (real.day >= as_of - pd.Timedelta(days=RECENT + BASELINE))]

    # SAFETY: volume does not matter
    for flag in ("child_safety", "legal"):
        hits = df[(df.day < as_of) & (df.day >= as_of - pd.Timedelta(days=1)) & df["flags"].str.contains(flag, na=False)]
        for app, g in hits.groupby("app"):
            alerts.append(dict(type="SAFETY", app=app, category=flag, key=f"SAFETY|{app}|{flag}",
                               evidence=dict(new_reports=len(g), review_ids=list(g.review_id))))

    if as_of < history_start + pd.Timedelta(days=RECENT + 14):
        return alerts            # not enough history yet for "normal" - only safety runs
    for app, g in recent.groupby("app"):
        n_now = len(g)
        b = base[base.app == app]
        n_base = len(b)
        if n_now < 30 or n_base < 100:
            continue
        for cat, k in g.category.value_counts().items():
            if cat in NOT_PROBLEMS or k < MIN_COUNT:
                continue
            p_now = k / n_now
            p_base = (b.category == cat).sum() / n_base
            lift = (p_now + 0.005) / (p_base + 0.005)            # smoothed: new categories don't divide by 0
            z = z_score(k, n_now, p_base)
            if lift >= MIN_LIFT and z >= MIN_Z:
                alerts.append(dict(type="SPIKE", app=app, category=cat, key=f"SPIKE|{app}|{cat}",
                                   evidence=dict(count_7d=int(k), share_now=round(p_now, 3),
                                                 share_normal=round(p_base, 3), lift=round(lift, 1), z=round(z, 1))))

    # RELEASE: newest version vs previous version, same app
    for app, g in real[real.day >= as_of - pd.Timedelta(days=RECENT + BASELINE)].groupby("app"):
        versions = sorted(g.version.dropna().unique(), key=lambda v: [int(x) for x in v.split(".")])
        if len(versions) < 2:
            continue
        new_v, old_v = versions[-1], versions[-2]
        new, old = g[g.version == new_v], g[g.version == old_v]
        if len(new) < 30 or len(old) < 100:
            continue
        for cat, k in new.category.value_counts().items():
            if cat in NOT_PROBLEMS or k < MIN_COUNT:
                continue
            p_new, p_old = k / len(new), (old.category == cat).sum() / len(old)
            lift, z = (p_new + 0.005) / (p_old + 0.005), z_score(k, len(new), p_old)
            if lift >= MIN_LIFT and z >= MIN_Z:
                # where is it concentrated? device share inside the problem vs in the whole version
                prob = new[new.category == cat]
                dev = (prob.device.value_counts(normalize=True) /
                       new.device.value_counts(normalize=True)).dropna().sort_values(ascending=False)
                hot = [f"{d} ({prob.device.value_counts(normalize=True)[d]:.0%} of reports, x{x:.1f})"
                       for d, x in dev.items() if x >= 1.5][:4]
                alerts.append(dict(type="RELEASE", app=app, category=cat, key=f"RELEASE|{app}|{cat}|{new_v}",
                                   evidence=dict(version=new_v, previous=old_v, count=int(k), share_new=round(p_new, 3),
                                                 share_previous=round(p_old, 3), lift=round(lift, 1), z=round(z, 1),
                                                 concentrated_on=hot)))

    # NEW_THEME: themes born yesterday (after the baseline period)
    born = themes[(themes.born_on == (as_of - pd.Timedelta(days=1)).strftime("%Y-%m-%d")) &
                  (themes.born_on > history_start.strftime("%Y-%m-%d"))]       # baseline themes aren't "new"
    for t in born.itertuples():
        if t.category in NOT_PROBLEMS:
            continue
        apps = json.loads(t.apps)
        alerts.append(dict(type="NEW_THEME", app=max(apps, key=apps.get), category=t.category, key=f"NEW_THEME|{t.theme_id}",
                           evidence=dict(theme=t.theme_id, size_at_birth=int(t.size), keywords=t.keywords, quote=t.quote)))
    return alerts


def slow_trends(df):
    """EMERGING and CHRONIC need the whole history, so they are computed once, weekly."""
    alerts = []
    real = df[df.is_noise == 0].copy()
    real["week"] = (real.day - real.day.min()).dt.days // 7
    weeks = sorted(real.week.unique())[:-1] or [0]          # drop the last, partial week
    real = real[real.week.isin(weeks)]
    for app, g in real.groupby("app"):
        tot = g.groupby("week").size()
        for cat in g.category.dropna().unique():
            if cat in NOT_PROBLEMS:
                continue
            share = (g[g.category == cat].groupby("week").size() / tot).reindex(tot.index, fill_value=0)
            if len(share) < 4:
                continue
            first, last = share.iloc[:2].mean(), share.iloc[-2:].mean()
            rising_weeks = sum(b > a for a, b in zip(share, share.iloc[1:]))
            if last >= 2 * max(first, 0.01) and rising_weeks >= len(share) * 0.6 and last >= 0.05:
                alerts.append(dict(type="EMERGING", app=app, category=cat, key=f"EMERGING|{app}|{cat}",
                                   evidence=dict(share_first_weeks=round(first, 3), share_last_weeks=round(last, 3),
                                                 weekly_share=[round(x, 3) for x in share])))
            elif share.min() >= 0.10 and share.max() / max(share.min(), 1e-3) < 1.8:
                alerts.append(dict(type="CHRONIC", app=app, category=cat, key=f"CHRONIC|{app}|{cat}",
                                   evidence=dict(avg_share=round(share.mean(), 3),
                                                 weekly_share=[round(x, 3) for x in share])))
    return alerts


PRIORITY = {"SAFETY": 100, "RELEASE": 80, "SPIKE": 60, "NEW_THEME": 50, "EMERGING": 30, "CHRONIC": 20}
SEVERE = {"child_safety", "legal", "payment_purchase", "progress_account_loss", "crash_stability", "chat_safety"}


def priority(a):
    p = PRIORITY[a["type"]] + (15 if a["category"] in SEVERE else 0)
    ev = a["evidence"]
    p += min(15, int(ev.get("lift", 0)))
    return p


def run():
    con = sqlite3.connect(DB)
    df = load(con)
    themes = pd.read_sql("SELECT * FROM themes", con)
    start, end = df.day.min(), df.day.max()

    # daily metrics (for charts)
    real = df[df.is_noise == 0]
    daily = real.groupby(["app", "category", "day"]).size().rename("count").reset_index()
    totals = real.groupby(["app", "day"]).size().rename("app_total").reset_index()
    daily = daily.merge(totals, on=["app", "day"])
    daily["share"] = (daily["count"] / daily["app_total"]).round(4)
    daily["day"] = daily.day.dt.strftime("%Y-%m-%d")
    daily.to_sql("daily_metrics", con, if_exists="replace", index=False)

    # replay every morning
    issues = {}
    day = start + pd.Timedelta(days=1)        # safety works from day 1; trend checks wait for enough history
    while day <= end + pd.Timedelta(days=1):
        for a in detect(df, themes, day, start):
            k = a["key"]
            if k not in issues:
                issues[k] = {**a, "first_raised": day.strftime("%Y-%m-%d")}
            issues[k]["last_seen"] = day.strftime("%Y-%m-%d")
            if a["type"] != "SAFETY":
                issues[k]["evidence"] = a["evidence"]          # keep the latest numbers
        day += pd.Timedelta(days=1)
    for a in slow_trends(df):
        issues[a["key"]] = {**a, "first_raised": end.strftime("%Y-%m-%d"), "last_seen": end.strftime("%Y-%m-%d")}

    # safety: replace evidence with the full list
    for k, a in issues.items():
        if a["type"] == "SAFETY":
            hits = df[(df.app == a["app"]) & df["flags"].str.contains(a["category"], na=False)]
            a["evidence"] = dict(total_reports=len(hits), first=hits.day.min().strftime("%Y-%m-%d"),
                                 review_ids=list(hits.review_id))

    rows = []
    for a in issues.values():
        a["priority"] = priority(a)
        rows.append((a["key"], a["type"], a["app"], a["category"], a["first_raised"], a["last_seen"],
                     a["priority"], json.dumps(a["evidence"], ensure_ascii=False)))
    con.execute("DROP TABLE IF EXISTS alerts")
    con.execute("""CREATE TABLE alerts (alert_key TEXT PRIMARY KEY, type TEXT, app TEXT, category TEXT,
                   first_raised TEXT, last_seen TEXT, priority INTEGER, evidence TEXT)""")
    con.executemany("INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()

    # ROLL UP: one issue per app x category. A product person should see
    # "Merge Meadow crashes on 2.4.0" once, with all the signals that point to it.
    grouped = {}
    for a in issues.values():
        grouped.setdefault((a["app"], a["category"]), []).append(a)
    roll = []
    for (app, cat), sigs in grouped.items():
        sigs.sort(key=lambda a: (-a["priority"], a["first_raised"]))
        lead = sigs[0]
        types = sorted({a["type"] for a in sigs}, key=lambda t: -PRIORITY[t])
        roll.append(dict(app=app, category=cat, lead=lead["type"], first_raised=min(a["first_raised"] for a in sigs),
                         priority=lead["priority"] + 5 * (len(types) - 1),     # more independent signals = more sure
                         signals=[f"{a['type']} {a['first_raised']}" for a in sorted(sigs, key=lambda a: a["first_raised"])],
                         themes=[a["evidence"]["theme"] for a in sigs if a["type"] == "NEW_THEME"],
                         evidence=lead["evidence"], types=types))
    con.execute("DROP TABLE IF EXISTS issues")
    con.execute("""CREATE TABLE issues (app TEXT, category TEXT, lead_signal TEXT, first_raised TEXT, priority INTEGER,
                   signals TEXT, themes TEXT, evidence TEXT)""")
    con.executemany("INSERT INTO issues VALUES (?,?,?,?,?,?,?,?)",
                    [(r["app"], r["category"], r["lead"], r["first_raised"], r["priority"], json.dumps(r["signals"]),
                      json.dumps(r["themes"]), json.dumps(r["evidence"], ensure_ascii=False)) for r in roll])
    con.commit()

    print(f"{len(rows)} signals -> {len(roll)} issues over {(end - start).days + 1} days\n")
    for r in sorted(roll, key=lambda r: (-r["priority"], r["first_raised"])):
        a = {"type": r["lead"], "evidence": r["evidence"], "priority": r["priority"], "first_raised": r["first_raised"],
             "app": r["app"], "category": r["category"]}
        ev = a["evidence"]
        if a["type"] == "SAFETY":
            detail = f"{ev['total_reports']} reports since {ev['first']}"
        elif a["type"] == "RELEASE":
            detail = (f"v{ev['version']}: {ev['share_new']:.0%} of reviews vs {ev['share_previous']:.0%} in "
                      f"v{ev['previous']} (x{ev['lift']}, n={ev['count']})  devices: {', '.join(ev['concentrated_on']) or '-'}")
        elif a["type"] == "SPIKE":
            detail = f"{ev['share_now']:.0%} of last 7 days vs normal {ev['share_normal']:.0%} (x{ev['lift']}, n={ev['count_7d']})"
        elif a["type"] == "NEW_THEME":
            detail = f"{ev['theme']} [{ev['keywords']}] \"{ev['quote'][:60]}\""
        elif a["type"] == "EMERGING":
            detail = f"weekly share {ev['share_first_weeks']:.0%} -> {ev['share_last_weeks']:.0%}"
        else:
            detail = f"steady ~{ev['avg_share']:.0%} of reviews every week"
        print(f"  [{a['priority']:3d}] {a['first_raised']}  {a['app']:13s} {a['category']:22s} {a['type']:9s} {detail}")
        extra = [t for t in r["types"] if t != r["lead"]]
        if extra or r["themes"]:
            print(f"        also: {', '.join(extra) or '-'}"
                  + (f"   themes: {', '.join(r['themes'])}" if r["themes"] else ""))
    con.close()


if __name__ == "__main__":
    run()
