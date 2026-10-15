"""
Live intake: one new review -> rules -> lane -> (urgent page) -> site/live.json

Runs inside GitHub Actions when someone opens a "Simulate a Play review" issue.
In production the same code runs on every new review the hourly poller finds.

It uses only the fast rule layer (noise filter, risk rules, keyword topics), so it
finishes in seconds and can't be "unsure" about a child-safety report. The daily
job later re-classifies the review with the multilingual model.

Usage:  python live/intake.py <issue_body_file> <issue_number> <issue_url>
Writes: site/live.json (appends), live/comment.md (posted back on the issue),
        live/urgent.flag (exists when the review paged people)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))
import classify   # noqa: E402  (rules only; the model is never loaded here)
import triage     # noqa: E402
import urgent     # noqa: E402

LIVE = os.path.join(ROOT, "site", "live.json")
OWNERS = json.load(open(os.path.join(ROOT, "config", "owners.json"), encoding="utf-8"))

# keyword topics for the fast path (the model does this properly in the daily run)
TOPIC_WORDS = [
    ("payment_purchase", r"paid|charged|bought|purchase|refund|bezahlt|abgebucht|gekauft|paguei|cobra|comprei|שילמתי|חויבתי|קניתי"),
    ("ads", r"\bads?\b|advert|werbung|anúncio|propaganda|פרסומ"),
    ("feature_request", r"please add|would love|wish there|bitte mehr|adicion|por favor, mais|תוסיפו"),
    ("difficulty_balance", r"too hard|stuck|zu schwer|difícil|קשה מדי|תקוע"),
    ("pricing_monetization", r"expensive|too much money|teuer|caro|יקר"),
    ("performance_battery", r"\blag|slow|battery|akku|bateria|לאט|סוללה"),
    ("praise", r"love|great|awesome|amazing|best|liebe|toll|super|amo|ótimo|adoro|מעולה|אוהב|כיף"),
]


def parse_issue(body):
    """GitHub issue forms render as '### Label\\n\\nvalue' blocks."""
    fields, cur = {}, None
    for line in body.splitlines():
        m = re.match(r"^###\s+(.*)", line)
        if m:
            cur = m.group(1).strip().lower()
            fields[cur] = []
        elif cur is not None:
            fields[cur].append(line)
    get = lambda k: "\n".join(fields.get(k, [])).strip()
    stars = re.search(r"\d", get("rating") or "")
    lang = re.search(r"\((\w\w)\)", get("language") or "")
    return dict(app=get("app") or "PeleGame", stars=int(stars.group()) if stars else 1,
                lang=lang.group(1) if lang else "en", version=get("app version") or "5.2.0",
                text=get("review text"))


def topic(text, flags):
    low = text.lower()
    if re.search(classify.PROGRESS_WORDS, low) and re.search(classify.LOSS_WORDS, low):
        return "progress_account_loss"
    if re.search(classify.CRASH_WORDS, low):
        return "crash_stability"
    for cat, pat in TOPIC_WORDS:
        if re.search(pat, low):
            return cat
    return "other"


def main(body_file, number, url):
    r = parse_issue(open(body_file, encoding="utf-8").read())
    now = datetime.now(timezone.utc).replace(microsecond=0)
    flags = classify.risk_flags(r["text"], None, r["stars"])
    noise = None if set(flags) & classify.PROTECTED_FLAGS else classify.noise_reason(r["text"])
    cat = None if noise else topic(r["text"], flags)
    row = dict(review_id=f"live-{number}", app=r["app"], text=r["text"], star_rating=r["stars"], first_star_rating=None,
               reviewer_language=r["lang"], version=r["version"], thumbs_up=0, is_noise=int(bool(noise)),
               noise_reason=noise, category=cat, category_score=0.9 if cat and cat != "other" else 0.0, flags=",".join(flags))
    lane, reason = triage.lane_for(row, set())
    team, sla = triage.TEAM[lane]
    draft = triage.draft_for(lane, reason, row)
    reply_status, violations = None, []
    if lane in ("AUTO", "INCIDENT"):
        violations = triage.policy_check(draft)
        reply_status = "posted" if not violations else "blocked by policy check -> person"

    # urgent page?
    live = json.load(open(LIVE, encoding="utf-8")) if os.path.exists(LIVE) else []
    page = None
    typ = next((t for t, cfg in urgent.TYPES.items() if cfg["flags"] & set(flags)), None)
    if typ:
        cfg = urgent.TYPES[typ]
        prev = [x for x in live if x.get("urgent") and x["urgent"]["type"] == typ and x["app"] == r["app"]
                and now - datetime.fromisoformat(x["received_at"]) <= urgent.MERGE_WINDOW]
        reports = len(prev) + 1
        recent = [x for x in prev if now - datetime.fromisoformat(x["received_at"]) <= timedelta(hours=24)]
        paged = [(role, OWNERS["contacts"][role][r["app"]] if role == "pm" else OWNERS["contacts"][role]) for role in cfg["roles"]]
        if len(recent) + 1 >= 3:
            paged.append(("head_of_product", OWNERS["contacts"]["head_of_product"]))
        due = now + timedelta(hours=cfg["sla_h"])
        page = dict(type=typ, label=cfg["label"], sla=cfg["sla_h"], due=due.isoformat(), paged=paged, policy=cfg["policy"],
                    actions=cfg["actions"], reports_in_window=reports, joined_open_alert=bool(prev))

    entry = dict(id=row["review_id"], issue=int(number), issue_url=url, received_at=now.isoformat(), app=r["app"],
                 stars=r["stars"], lang=r["lang"], version=r["version"], text=r["text"], flags=flags, noise=noise,
                 category=cat, lane=lane, reason=reason, team=team, sla=sla, draft=draft, reply_status=reply_status,
                 violations=violations, urgent=page)
    live.append(entry)
    json.dump(live, open(LIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # the comment posted back on the issue = the page the owners receive
    gh = OWNERS.get("github_pm", {}).get(r["app"]) or os.environ.get("REPO_OWNER", "")
    lines = [f"**Review received** · {r['app']} · {r['stars']}★ · {r['lang']} · v{r['version']}", "",
             f"> {r['text']}", "",
             f"**Lane:** {lane} — {reason}  ", f"**Handled by:** {team}" + (f" · reply within {sla} h" if sla else "")]
    if flags:
        lines.append(f"**Risk flags (rules):** {', '.join(flags)}")
    if page:
        lines += ["", f"## 🚨 URGENT · {page['label']} · {r['app']}",
                  f"Paged now: " + ", ".join(f"{urgent_role(role)} ({addr})" for role, addr in page["paged"]),
                  f"Respond by **{due:%d %b %H:%M} UTC** ({page['sla']} h). At risk: {page['policy']}.",
                  "Next steps: " + "; ".join(page["actions"]) + ".",
                  "Automatic reply blocked; Legal approves the answer.",
                  f"{'@' + gh + ' ' if gh else ''}(product manager on call for {r['app']})"]
    elif draft and reply_status:
        lines += ["", f"**Automatic reply** ({reply_status}):", f"> {draft}"]
    elif draft:
        lines += ["", f"**Suggested draft for the person:**", f"> {draft}"]
    lines += ["", "_The live dashboard updates in about a minute._"]
    open(os.path.join(ROOT, "live", "comment.md"), "w", encoding="utf-8").write("\n".join(lines))
    flag_path = os.path.join(ROOT, "live", "urgent.flag")
    if page:
        open(flag_path, "w").write(page["type"])
    elif os.path.exists(flag_path):
        os.remove(flag_path)
    print(json.dumps(entry, ensure_ascii=False, indent=1))


ROLE = {"pm": "Product manager", "legal": "Legal on-call", "trust_safety": "Trust & Safety", "dpo": "Privacy officer",
        "ad_ops": "Ad monetization", "support_lead": "Support lead", "head_of_product": "Head of Product"}


def urgent_role(role):
    return ROLE.get(role, role)


if __name__ == "__main__":
    main(*sys.argv[1:4])
