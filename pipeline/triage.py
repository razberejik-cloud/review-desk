"""
Step 6 - Triage: who answers what, and safe automatic replies
=============================================================

Every review goes to exactly ONE lane. The lanes are checked from the most
serious down, so a review can never "fall" into automation if any serious
signal is present.

  1. SAFETY       child safety / chat safety     -> Trust & Safety + Legal notified now,
                                                    a person replies (SLA 2h). Never automatic.
  2. LEGAL        legal threat / privacy           -> Legal notified, a person replies (SLA 24h)
  3. HUMAN_VIP    money, lost progress, high-value player, 5->1 rating drop
                                                  -> support agent, with a draft to edit (SLA 24h)
  4. HUMAN_CHECK  the system is unsure (low score / angry but unclassified)
                                                  -> person decides (SLA 48h)
  5. INCIDENT     review about an active release issue (step 5)
                                                  -> one approved incident reply, edited again when
                                                     the fix ships (Play lets you edit a reply)
  6. AUTO         praise, feature ideas, known chronic topics
                                                  -> template reply in the reviewer's language
  7. NO_REPLY     noise (counted only); spam is reported to Google

Every automatic reply must pass the POLICY GUARD below, or it is not posted
and goes to a person instead. 5% of automatic replies are also sampled for a
human quality check.

Policy source: Google Play "Ratings, Reviews, and Installs" policy - don't
ask for a higher rating, no rewards for ratings, no coupons / codes / links /
email addresses in review content, point users to support resources.
API limits: 350 characters, plain text, 2,000 replies per app per day.

Output (in reviews.db):
  triage         review_id, lane, reason, priority, team, sla_hours, draft, reply_status, violations
  notifications  team, review_id, sent_on, message   (who was told about what)
  reviews.developer_reply / replied_at   filled for replies that were "posted"

Run after trends.py:  python triage.py
"""

import json
import random
import re
import sqlite3
from collections import Counter, defaultdict

DB = "reviews.db"
SUPPORT_URL = "support.example.com"       # the only link a reply may contain
MAX_LEN = 350
DAILY_QUOTA = 2000
QA_SAMPLE = 0.05
LOW_CONFIDENCE = 0.40
rng = random.Random(1)

# ---------------------------------------------------------------------------
# Approved templates (written once, reviewed by legal/brand, in 4 languages)
# ---------------------------------------------------------------------------
T = {
    "praise": {
        "en": "Thank you so much for playing {app}! We're really happy you're enjoying it.",
        "de": "Vielen Dank, dass du {app} spielst! Wir freuen uns sehr, dass es dir gefällt.",
        "pt": "Muito obrigado por jogar {app}! Ficamos muito felizes que você esteja curtindo.",
        "he": "תודה רבה שאתם משחקים ב־{app}! אנחנו ממש שמחים שאתם נהנים.",
    },
    "feature_request": {
        "en": "Thanks for the idea! We've shared it with the {app} team, player suggestions really shape what we build next.",
        "de": "Danke für die Idee! Wir haben sie an das {app}-Team weitergegeben. Vorschläge von Spielern prägen, was wir als Nächstes bauen.",
        "pt": "Obrigado pela ideia! Já compartilhamos com a equipe do {app}, as sugestões dos jogadores ajudam a definir o que vem a seguir.",
        "he": "תודה על הרעיון! העברנו אותו לצוות של {app}. ההצעות של השחקנים באמת משפיעות על מה שנבנה בהמשך.",
    },
    "ads": {
        "en": "Thanks for the honest feedback about ads. We're reviewing how often they appear. If you bought ad removal and still see ads, please contact us at " + SUPPORT_URL + " so we can fix it.",
        "de": "Danke für dein ehrliches Feedback zur Werbung. Wir prüfen gerade, wie oft sie erscheint. Falls du Werbefreiheit gekauft hast und trotzdem Werbung siehst, melde dich bitte unter " + SUPPORT_URL + ".",
        "pt": "Obrigado pelo feedback sincero sobre os anúncios. Estamos revisando a frequência deles. Se você comprou a remoção de anúncios e ainda os vê, fale conosco em " + SUPPORT_URL + ".",
        "he": "תודה על המשוב הכן לגבי הפרסומות. אנחנו בודקים את התדירות שלהן. אם רכשתם הסרת פרסומות ועדיין רואים אותן, פנו אלינו ב־" + SUPPORT_URL + ".",
    },
    "difficulty_balance": {
        "en": "Thanks for letting us know! Level balance is something we keep tuning, and your feedback goes straight to the design team.",
        "de": "Danke für dein Feedback! Wir passen die Schwierigkeit der Level laufend an, und deine Rückmeldung geht direkt an das Design-Team.",
        "pt": "Obrigado por avisar! Estamos sempre ajustando a dificuldade das fases, e seu feedback vai direto para a equipe de design.",
        "he": "תודה שסיפרתם לנו! אנחנו ממשיכים לכוונן את רמת הקושי של השלבים, והמשוב שלכם עובר ישירות לצוות העיצוב.",
    },
    "pricing_monetization": {
        "en": "Thanks for the feedback on prices. We want {app} to feel fair for everyone, and we've passed your comments to the team.",
        "de": "Danke für dein Feedback zu den Preisen. {app} soll sich für alle fair anfühlen, deshalb haben wir deine Hinweise ans Team weitergegeben.",
        "pt": "Obrigado pelo feedback sobre os preços. Queremos que {app} seja justo para todos e repassamos seus comentários à equipe.",
        "he": "תודה על המשוב לגבי המחירים. חשוב לנו ש־{app} ירגיש הוגן לכולם, והעברנו את ההערות שלכם לצוות.",
    },
    "content_repetitive": {
        "en": "Thanks for playing so much of {app}! New content is on the way, and your feedback helps us decide what comes next.",
        "de": "Danke, dass du so viel {app} spielst! Neue Inhalte sind unterwegs, und dein Feedback hilft uns bei der Planung.",
        "pt": "Obrigado por jogar tanto {app}! Novos conteúdos estão a caminho e seu feedback nos ajuda a decidir o que vem depois.",
        "he": "תודה שאתם משחקים כל כך הרבה ב־{app}! תוכן חדש בדרך, והמשוב שלכם עוזר לנו להחליט מה יגיע בהמשך.",
    },
    "performance_battery": {
        "en": "Sorry about the performance trouble. Updating to the latest version and closing other apps can help. If it continues, please write to " + SUPPORT_URL + " with your device model.",
        "de": "Entschuldige die Probleme mit der Leistung. Ein Update auf die neueste Version und das Schließen anderer Apps kann helfen. Sonst schreib uns bitte an " + SUPPORT_URL + ".",
        "pt": "Desculpe pelos problemas de desempenho. Atualizar para a versão mais recente e fechar outros apps pode ajudar. Se continuar, escreva para " + SUPPORT_URL + ".",
        "he": "מצטערים על בעיות הביצועים. עדכון לגרסה האחרונה וסגירת אפליקציות אחרות יכולים לעזור. אם זה ממשיך, כתבו לנו ב־" + SUPPORT_URL + ".",
    },
    "multiplayer_servers": {
        "en": "Sorry about the lag. We're working on server capacity for busy hours. If you keep getting disconnected, please let us know at " + SUPPORT_URL + ".",
        "de": "Entschuldige die Verzögerungen. Wir arbeiten an mehr Serverkapazität für Stoßzeiten. Wenn du oft getrennt wirst, melde dich bitte unter " + SUPPORT_URL + ".",
        "pt": "Desculpe pela lentidão. Estamos aumentando a capacidade dos servidores nos horários de pico. Se continuar caindo, fale conosco em " + SUPPORT_URL + ".",
        "he": "מצטערים על האיטיות. אנחנו מגדילים את קיבולת השרתים בשעות העומס. אם החיבור ממשיך להתנתק, ספרו לנו ב־" + SUPPORT_URL + ".",
    },
    "incident_fixed": {
        "en": "Update: the fix is out in version {fixed}. Please update the game from Google Play. If you still have trouble, write to us at " + SUPPORT_URL + ".",
        "de": "Update: Die Lösung ist in Version {fixed} verfügbar. Bitte aktualisiere das Spiel über Google Play. Falls es weiter Probleme gibt, schreib uns an " + SUPPORT_URL + ".",
        "pt": "Atualização: a correção já está na versão {fixed}. Atualize o jogo pela Google Play. Se ainda tiver problemas, fale conosco em " + SUPPORT_URL + ".",
        "he": "עדכון: התיקון זמין בגרסה {fixed}. עדכנו את המשחק דרך Google Play. אם עדיין יש בעיה, כתבו לנו ב־" + SUPPORT_URL + ".",
    },
    "incident": {
        "en": "Sorry about this! We know about the issue in version {version} on some devices and the team is working on a fix. We'll update this reply when it's out.",
        "de": "Das tut uns leid! Wir kennen das Problem in Version {version} auf einigen Geräten und arbeiten an einer Lösung. Wir aktualisieren diese Antwort, sobald sie verfügbar ist.",
        "pt": "Sentimos muito! Já sabemos do problema na versão {version} em alguns aparelhos e a equipe está trabalhando na correção. Vamos atualizar esta resposta quando sair.",
        "he": "מצטערים על זה! אנחנו מכירים את הבעיה בגרסה {version} בחלק מהמכשירים, והצוות עובד על תיקון. נעדכן את התשובה הזו כשהוא ייצא.",
    },
}
# drafts for people (never auto-posted; an agent edits and sends)
DRAFTS = {
    "SAFETY": "Thank you for telling us, and we're very sorry. Keeping kids safe is our top priority. Our safety team is reviewing this now. Please also use Report in the player's profile so we can act on the account. We'd like to follow up with you at " + SUPPORT_URL + ".",
    "LEGAL": "Thank you for raising this. We take it seriously and our team will look into it. Please contact us at " + SUPPORT_URL + " so we can follow up with you directly.",
    "payment": "Sorry about the purchase problem. Please open Settings > Support in the game so we can see your order and fix it. Never share card details in a review.",
    "progress": "We're sorry your progress is missing. Please open Settings > Support in the game. With your player ID our team can look into restoring it.",
    "default": "Thanks for the detailed feedback, and sorry for the trouble. Our team is looking into it. You can reach us anytime at " + SUPPORT_URL + ".",
}

# ---------------------------------------------------------------------------
# POLICY GUARD - every automatic reply must pass ALL checks
# ---------------------------------------------------------------------------
CHECKS = [
    ("too_long", lambda r: len(r) > MAX_LEN),
    ("html", lambda r: bool(re.search(r"<[^>]+>", r))),
    ("asks_for_rating", lambda r: bool(re.search(
        r"(rating|stars?|review|bewertung|sterne|avalia|estrelas|דירוג|כוכבים).{0,40}(change|update|raise|higher|änder|erhöh|mud|aument|לשנות|להעלות)|"
        r"(change|update|raise|änder|mude|atualize|שנו|תעלו).{0,40}(rating|stars?|review|bewertung|sterne|avalia|estrelas|דירוג|כוכבים)", r, re.I))),
    ("incentive", lambda r: bool(re.search(
        r"(free|gratis|grátis|חינם|בחינם|reward|belohnung|recompensa|(?<![א-ת])פרס(?![א-ת])|bonus|coins|gems|münzen|moedas|מטבעות|code|código|קוד|discount|rabatt|desconto|הנחה)", r, re.I))),
    ("foreign_link", lambda r: any(SUPPORT_URL not in u for u in re.findall(r"(?:https?://)?(?:[\w-]+\.)+(?:com|net|org|io|app|example|ly)\S*", r))),
    ("email_address", lambda r: bool(re.search(r"[\w.+-]+@[\w-]+\.\w+", r))),
    ("asks_personal_data", lambda r: bool(re.search(
        r"password|passwort|senha|סיסמ|card number|kartennummer|número do cartão|מספר כרטיס|phone number|telefon|telefone|טלפון|home address|adresse|endereço|כתובת מגורים", r, re.I))),
    ("promises_money", lambda r: bool(re.search(r"refund|erstatt|reembols|החזר", r, re.I))),
    ("blames_user", lambda r: bool(re.search(r"your fault|you did it wrong|deine schuld|culpa sua|אשמתך", r, re.I))),
]


def policy_check(reply):
    return [name for name, bad in CHECKS if bad(reply)]


# ---------------------------------------------------------------------------
def lane_for(r, active_incidents):
    flags = set((r["flags"] or "").split(",")) - {""}
    cat, score, rating = r["category"], r["category_score"], r["star_rating"]
    if r["is_noise"]:
        return ("NO_REPLY", "spam - reported to Google" if r["noise_reason"] == "spam" else f"noise: {r['noise_reason']}")
    if "child_safety" in flags:
        return ("SAFETY", "child safety report")
    if cat == "chat_safety":
        # the MODEL thinks it's about safety but the RULES found nothing: a person checks, Legal is NOT paged.
        # (real example from this data: Hebrew "my kids are dying for it" = "my kids love it")
        return ("HUMAN_CHECK", "possible safety topic - verify")
    if flags & {"legal", "privacy", "store_policy"}:
        names = {"legal": "legal threat", "privacy": "privacy / kids' data", "store_policy": "store-policy risk"}
        return ("LEGAL", " + ".join(names[f] for f in sorted(flags & set(names))))
    vip = []
    if "payment" in flags or cat == "payment_purchase":
        vip.append("payment")
    if cat == "progress_account_loss":
        vip.append("lost progress")
    if "high_value" in flags:
        vip.append("high-value player")
    if "rating_drop" in flags:
        vip.append(f"rating drop {r['first_star_rating']}->{rating}")
    if vip:
        return ("HUMAN_VIP", ", ".join(vip))
    if (r["app"], cat, r["version"]) in active_incidents:
        return ("INCIDENT", f"known issue: {cat} in v{r['version']}")
    if cat == "crash_stability":           # before the confidence check: a crash is never "unclear"
        return ("HUMAN_CHECK", "crash outside a known incident" + (" (5-star review!)" if rating >= 4 else ""))
    if cat == "other" or score is None or score < LOW_CONFIDENCE:
        if rating <= 2:
            return ("HUMAN_CHECK", "angry but unclear topic")
        return ("NO_REPLY", "unclear topic, not negative")
    if cat in T:
        return ("AUTO", f"template: {cat}")
    return ("HUMAN_CHECK", f"no approved template for {cat}")


TEAM = {"SAFETY": ("Trust & Safety + Legal", 1), "LEGAL": ("Legal", 24), "HUMAN_VIP": ("Player Support", 24),
        "HUMAN_CHECK": ("Community team", 48), "INCIDENT": ("auto", 0), "AUTO": ("auto", 0), "NO_REPLY": ("-", 0)}
BASE_PRIORITY = {"SAFETY": 100, "LEGAL": 80, "HUMAN_VIP": 60, "HUMAN_CHECK": 30}


def priority(lane, r, reason=""):
    if lane not in BASE_PRIORITY:
        return 0
    p = BASE_PRIORITY[lane] + (40 if "possible safety" in reason else 0)
    p += min(15, (r["thumbs_up"] or 0) // 2)                  # public exposure: others agreed with it
    p += 10 if "high_value" in (r["flags"] or "") else 0
    p += 5 if "rating_drop" in (r["flags"] or "") else 0
    return p


def draft_for(lane, reason, r):
    lang = r["reviewer_language"] if r["reviewer_language"] in ("en", "de", "pt", "he") else "en"
    if lane == "AUTO":
        return T[r["category"]][lang].format(app=r["app"])
    if lane == "INCIDENT":
        return T["incident"][lang].format(version=r["version"])
    if lane in ("SAFETY", "LEGAL"):
        return DRAFTS[lane]
    if lane == "HUMAN_VIP":
        return DRAFTS["payment" if "payment" in reason else "progress" if "progress" in reason else "default"]
    if lane == "HUMAN_CHECK":
        return DRAFTS["default"]
    return None


# ---------------------------------------------------------------------------
def run():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("""
        SELECT r.review_id, a.app_name AS app, r.text, r.star_rating, r.first_star_rating, r.reviewer_language,
               r.app_version_name AS version, r.thumbs_up, substr(r.last_modified_at,1,10) AS day,
               ra.is_noise, ra.noise_reason, ra.category, ra.category_score, ra.flags
        FROM reviews r JOIN apps a USING(package_name) JOIN review_analysis ra USING(review_id)
        ORDER BY r.last_modified_at""")]

    # incidents from step 5: (app, category, version) that had a RELEASE alert, active from first_raised
    incidents = {}
    for key, first in con.execute("SELECT alert_key, first_raised FROM alerts WHERE type='RELEASE'"):
        _, app, cat, ver = key.split("|")
        incidents[(app, cat, ver)] = first

    out, notes, posted_per_app_day = [], [], Counter()
    for r in rows:
        active = {k for k, first in incidents.items() if r["day"] >= first}
        # reviews written before the alert existed are handled when the incident opens
        lane, reason = lane_for(r, set(incidents))
        if lane == "INCIDENT":
            open_day = incidents[(r["app"], r["category"], r["version"])]
            r["reply_day"] = max(r["day"], open_day)
        else:
            r["reply_day"] = r["day"]
        team, sla = TEAM[lane]
        draft = draft_for(lane, reason, r)
        violations, status = [], "none"
        if lane in ("AUTO", "INCIDENT"):
            violations = policy_check(draft)
            quota_key = (r["app"], r["reply_day"])
            if violations:
                status, lane = "blocked_by_policy -> person", "HUMAN_CHECK"
            elif posted_per_app_day[quota_key] >= DAILY_QUOTA:
                status = "deferred_quota"
            else:
                posted_per_app_day[quota_key] += 1
                status = "posted"
                if rng.random() < QA_SAMPLE:
                    status = "posted + QA sample"
        elif lane in BASE_PRIORITY:
            status = "queued_for_person"
        if lane in ("SAFETY", "LEGAL"):
            for t in team.split(" + "):
                notes.append((t, r["review_id"], r["day"], f"{lane}: {r['app']} - {r['text'][:120]}"))
        out.append((r["review_id"], lane, reason, priority(lane, r, reason), team, sla, draft, status, ",".join(violations)))

    con.execute("DROP TABLE IF EXISTS triage")
    con.execute("""CREATE TABLE triage (review_id TEXT PRIMARY KEY, lane TEXT, reason TEXT, priority INTEGER, team TEXT,
                   sla_hours INTEGER, draft TEXT, reply_status TEXT, violations TEXT)""")
    con.executemany("INSERT INTO triage VALUES (?,?,?,?,?,?,?,?,?)", out)
    con.execute("DROP TABLE IF EXISTS notifications")
    con.execute("CREATE TABLE notifications (team TEXT, review_id TEXT, sent_on TEXT, message TEXT)")
    con.executemany("INSERT INTO notifications VALUES (?,?,?,?)", notes)
    # INCIDENT FOLLOW-UP: when a newer version ships and the problem's share drops back, edit every incident reply
    followups = []
    for (app, cat, ver), opened in incidents.items():
        later = sorted({r["version"] for r in rows if r["app"] == app and r["version"] and
                        [int(x) for x in r["version"].split(".")] > [int(x) for x in ver.split(".")]})
        for nv in later:
            nv_rows = [r for r in rows if r["app"] == app and r["version"] == nv and not r["is_noise"]]
            if len(nv_rows) >= 50 and sum(r["category"] == cat for r in nv_rows) / len(nv_rows) < 0.05:
                fixed_on = min(r["day"] for r in nv_rows)
                followups.append((app, cat, ver, nv, fixed_on))
                break
    by_id = {r["review_id"]: r for r in rows}
    edited = 0
    for app, cat, ver, nv, fixed_on in followups:
        for i, o in enumerate(out):
            r = by_id[o[0]]
            if o[1] == "INCIDENT" and r["app"] == app and r["version"] == ver and o[7].startswith("posted"):
                lang = r["reviewer_language"] if r["reviewer_language"] in T["incident_fixed"] else "en"
                new = T["incident_fixed"][lang].format(fixed=nv)
                if not policy_check(new):
                    out[i] = o[:6] + (new,) + (o[7] + f", edited {fixed_on} (fixed in {nv})", o[8])
                    edited += 1
    print(f"Incident follow-ups: {[(a, f'v{v} fixed in v{nv} on {d}') for a, _, v, nv, d in followups]}, "
          f"{edited} replies edited\n")
    con.execute("DELETE FROM triage")
    con.executemany("INSERT INTO triage VALUES (?,?,?,?,?,?,?,?,?)", out)

    # simulate posting
    posted = {o[0]: o[6] for o in out if o[7].startswith("posted")}
    day_of = {r["review_id"]: r["reply_day"] for r in rows}
    con.executemany("UPDATE reviews SET developer_reply=?, replied_at=? WHERE review_id=?",
                    [(txt, day_of[rid], rid) for rid, txt in posted.items()])
    con.commit()
    report(con, rows, out, posted_per_app_day, notes)
    con.close()


def report(con, rows, out, quota, notes):
    n = len(out)
    lanes = Counter(o[1] for o in out)
    print(f"=== {n:,} reviews over 60 days ===\n")
    order = ["SAFETY", "LEGAL", "HUMAN_VIP", "HUMAN_CHECK", "INCIDENT", "AUTO", "NO_REPLY"]
    for l in order:
        print(f"  {l:12s} {lanes[l]:6,}  ({lanes[l] / n:5.1%})   -> {TEAM[l][0]}")
    human = sum(lanes[l] for l in BASE_PRIORITY)
    auto = lanes["AUTO"] + lanes["INCIDENT"]
    print(f"\n  handled without a person: {(auto + lanes['NO_REPLY']) / n:.0%}   "
          f"people needed for: {human:,} reviews (~{human / 60:.0f} a day instead of ~{n / 60:.0f})")
    st = Counter(o[7] for o in out)
    print(f"  automatic replies posted: {st['posted'] + st['posted + QA sample']:,}  "
          f"(QA sample for a person: {st['posted + QA sample']})  blocked by policy guard: "
          f"{sum(1 for o in out if o[7].startswith('blocked'))}")
    print(f"  busiest app-day for automatic replies: {max(quota.values())} of {DAILY_QUOTA} allowed")
    print(f"  notifications sent: {dict(Counter(t for t, *_ in notes))}")

    print("\nTop of the human queue:")
    texts = {r["review_id"]: r for r in rows}
    for o in sorted([o for o in out if o[1] in BASE_PRIORITY], key=lambda o: -o[3])[:6]:
        r = texts[o[0]]
        print(f"  [{o[3]:3d}] {o[1]:11s} {r['app']:12s} {r['star_rating']}★  {o[2]:28s} \"{r['text'][:70]}\"")

    print("\nPolicy guard self-test (replies that must be blocked):")
    bad = ["Sorry! Please update your rating to 5 stars once it's fixed.",
           "Thanks! Here's a code for 100 free coins: GIFT100",
           "Contact us at help@example.com",
           "Download our other game at othergame.example!",
           "Send us your password and we'll check your account.",
           "We will refund you right away.",
           "Bitte ändere deine Bewertung auf 5 Sterne.",
           "תעלו את הדירוג ונשלח לכם מטבעות בחינם",
           "x" * 400]
    for b in bad:
        print(f"  {'BLOCKED' if policy_check(b) else 'PASSED!!'}  {policy_check(b)}  \"{b[:55]}\"")
    clean = sum(not policy_check(t[l].format(app="PeleGame", version="2.4.0", fixed="2.4.1")) for t in T.values() for l in t)
    print(f"  all {sum(len(t) for t in T.values())} approved templates pass: {clean == sum(len(t) for t in T.values())}")


if __name__ == "__main__":
    run()
