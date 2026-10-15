"""
Check step 3 against the planted ground truth (the pipeline never reads this file).
Run after classify.py:   python evaluate.py
"""
import csv
import json
import sqlite3
from collections import Counter, defaultdict

EXPECTED_CATEGORY = {
    "version_crash": "crash_stability", "rating_mismatch": "crash_stability",
    "progress_lost": "progress_account_loss", "iap_not_received": "payment_purchase",
    "ads_chronic": "ads", "feature_avatars": "feature_request", "child_safety": "chat_safety",
}
EXPECTED_FLAG = {"child_safety": "child_safety", "iap_not_received": "payment", "high_value": "high_value"}

truth = {r["review_id"]: r["true_scenario"] for r in csv.DictReader(open("data/ground_truth.csv", encoding="utf-8"))}
con = sqlite3.connect("reviews.db")
pred = {rid: (noise, reason, cat, (flags or "").split(","), mm) for rid, noise, reason, cat, flags, mm in
        con.execute("SELECT review_id, is_noise, noise_reason, category, flags, rating_mismatch FROM review_analysis")}

# noise
is_noise_true = {rid for rid, s in truth.items() if s.startswith("noise")}
is_noise_pred = {rid for rid, p in pred.items() if p[0]}
tp = len(is_noise_true & is_noise_pred)
print(f"NOISE   caught {tp}/{len(is_noise_true)} ({tp / len(is_noise_true):.0%})   "
      f"false alarms {len(is_noise_pred - is_noise_true)} real reviews wrongly dropped")
missed = Counter(truth[r] for r in is_noise_true - is_noise_pred)
if missed:
    print("        missed by type:", dict(missed))

# categories
print("\nCATEGORY  (planted scenario -> share that got the expected category)")
by = defaultdict(Counter)
for rid, s in truth.items():
    if s in EXPECTED_CATEGORY:
        by[s][pred[rid][2]] += 1
for s, cnt in by.items():
    tot = sum(cnt.values())
    ok = cnt[EXPECTED_CATEGORY[s]]
    others = ", ".join(f"{c} {n}" for c, n in cnt.most_common(4) if c != EXPECTED_CATEGORY[s])
    print(f"  {s:18s} {ok}/{tot} ({ok / tot:.0%})   {('else: ' + others) if others else ''}")

# error analysis: show a few misses so we can see WHY
texts = dict(con.execute("SELECT review_id, text FROM reviews"))
for s in ("rating_mismatch", "progress_lost", "feature_avatars"):
    miss = [rid for rid, t in truth.items() if t == s and pred[rid][2] != EXPECTED_CATEGORY[s]][:3]
    for rid in miss:
        print(f"    miss [{s} -> {pred[rid][2]}] {texts[rid][:90]}")

# flags (recall is what matters here)
print("\nRISK FLAGS  (must not miss)")
for s, flag in EXPECTED_FLAG.items():
    ids = [rid for rid, t in truth.items() if t == s]
    hit = sum(flag in pred[rid][3] for rid in ids)
    print(f"  {s:18s} -> {flag:13s} {hit}/{len(ids)} ({hit / len(ids):.0%})")
cs_flagged = [rid for rid, p in pred.items() if "child_safety" in p[3]]
print(f"  child_safety flags raised in total: {len(cs_flagged)} "
      f"(real: {sum(truth[r] == 'child_safety' for r in cs_flagged)}, extra for a human to glance at: "
      f"{sum(truth[r] != 'child_safety' for r in cs_flagged)})")
mm = [rid for rid, t in truth.items() if t == "rating_mismatch"]
print(f"\nRATING MISMATCH  caught {sum(pred[r][4] for r in mm)}/{len(mm)}")

# ---------------------------------------------------------------------------
# THEMES (step 4): were planted problems discovered as themes, and how fast?
try:
    th = {tid: (born, size, kw) for tid, born, size, kw in con.execute("SELECT theme_id, born_on, size, keywords FROM themes")}
    rt = dict(con.execute("SELECT review_id, theme_id FROM review_themes"))
    first_day = {}
    for rid, day in con.execute("SELECT review_id, substr(last_modified_at,1,10) FROM reviews"):
        s_ = truth[rid]
        first_day[s_] = min(first_day.get(s_, day), day)
    print("\nTHEMES  (planted problem -> which themes hold it, and when the first theme was born)")
    for s_ in ("version_crash", "progress_lost", "iap_not_received", "feature_avatars", "ads_chronic"):
        ids = [rid for rid, t in truth.items() if t == s_ and rid in rt]
        tids = Counter(rt[rid] for rid in ids if rt[rid])
        covered = sum(tids.values())
        top = tids.most_common(3)
        born = min((th[t][0] for t, _ in top), default="-")
        purity = ", ".join(f"{t}: {n}/{th[t][1]}" for t, n in top)
        print(f"  {s_:17s} started {first_day[s_]}  first theme {born}  in themes {covered}/{len(ids)}  top: {purity}")
except sqlite3.OperationalError:
    pass

# ---------------------------------------------------------------------------
# ALERTS (step 5): did each planted problem become an issue, and how many days after it started?
PLANTED = [("version_crash", "Merge Meadow", "crash_stability"), ("progress_lost", "Tile Twist", "progress_account_loss"),
           ("iap_not_received", "PeleGame", "payment_purchase"), ("child_safety", "PeleGame", "child_safety"),
           ("feature_avatars", "PeleGame", "feature_request"), ("ads_chronic", "Smash Runner", "ads")]
try:
    issues = {(a, c): (lead, first, sig) for a, c, lead, first, sig in
              con.execute("SELECT app, category, lead_signal, first_raised, signals FROM issues")}
    from datetime import date as _d
    print("\nALERTS  (planted problem -> issue raised?  days from first review to alert)")
    for s_, app, cat in PLANTED:
        start = first_day.get(s_)
        if (app, cat) in issues:
            lead, first, sig = issues[(app, cat)]
            delay = (_d.fromisoformat(first) - _d.fromisoformat(start)).days
            print(f"  {s_:17s} YES  {lead:9s} started {start} -> raised {first}  ({delay} days)  signals: {', '.join(json.loads(sig))}")
        else:
            print(f"  {s_:17s} NO   (started {start})")
    false_alarms = [k for k in issues if k not in {(a, c) for _, a, c in PLANTED}]
    print(f"  other issues raised (check if real or false alarms): {false_alarms}")
except sqlite3.OperationalError:
    pass
