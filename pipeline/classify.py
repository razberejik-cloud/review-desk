"""
Step 3 - Filter noise, flag risk, classify every review (free, runs locally)
============================================================================

Three layers, in this order:

  A. NOISE FILTER (simple rules)
     too short / emoji only / gibberish / spam (links, "free coins")
     -> counted in volume stats, excluded from clustering. Spam is also
        marked "report to Google".

  B. RISK FLAGS (keyword rules, deliberately over-sensitive)
     child_safety, payment, legal, privacy, high_value
     Rules, not a model, because here missing one is worse than a false
     alarm. A flag forces the review to a human in step 6, whatever its category.

  C. CATEGORY (multilingual embeddings, no API, no cost)
     Every review becomes a vector ("a point on the map of meaning").
     Every category is described by a handful of example sentences.
     The review gets the category whose examples it is closest to.
     Works across languages: the examples are in English, the reviews are in
     Hebrew, German, Portuguese... the model maps them to the same place.
     Reviews close to no category -> "other" (or "off_topic" if they are
     closest to the off-topic examples).

Output (in reviews.db):
  review_analysis  one row per review: is_noise, noise_reason, category,
                   category_score, flags, rating_mismatch
  embeddings.npy + embedding_ids.json   vectors reused by step 4 (clustering)

Run in Colab:
  !pip install -q sentence-transformers
  then run this file (reviews.db and data/ must be next to it)
"""

import json
import re
import sqlite3
import unicodedata

import numpy as np

DB = "reviews.db"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"   # free, 50+ languages, ~470MB
MIN_SCORE = 0.35        # below this similarity -> "other"

# ---------------------------------------------------------------------------
# Taxonomy. "shared" applies to every app; genre families add their own.
# The example sentences are written independently of the fake data, in plain
# words a player would use. Adding a category = adding a few sentences.
# ---------------------------------------------------------------------------
TAXONOMY = {
    "shared": {
        "crash_stability": ["The app crashes when I open it", "Game closes by itself", "Stuck on a black screen",
                            "It freezes and shuts down", "Won't launch after updating"],
        "performance_battery": ["The game is slow and laggy", "My phone gets hot and the battery drains",
                                "Low frame rate, very choppy", "Takes too long to load"],
        "progress_account_loss": ["I lost all my progress", "My saved game was deleted", "My account was reset to level one",
                                  "Can't log in to my account anymore"],
        "payment_purchase": ["I paid but did not receive the item", "Money was charged but nothing arrived",
                             "Purchase failed and I want a refund", "I was charged twice"],
        "ads": ["Too many advertisements", "An ad after every level", "Ads that cannot be skipped",
                "I paid for no ads but still see ads"],
        "pricing_monetization": ["Items are too expensive", "Everything costs real money", "Pay to win",
                                 "Boosters cost too much"],
        "difficulty_balance": ["The levels are too hard", "I'm stuck on a level", "Lives recharge too slowly",
                               "Impossible without paying"],
        "content_repetitive": ["The levels feel repetitive", "Boring after a while", "Need more levels and content",
                               "I finished everything, nothing new"],
        "feature_request": ["Please add a new feature", "I wish there was an option to", "Would love more customization",
                            "Please add more options"],
        "praise": ["I love this game", "Great fun and relaxing", "Best game on my phone", "Beautiful graphics, highly recommend"],
    },
    "meta_world": {
        "chat_safety": ["A stranger sent private messages to my child", "An adult player is talking to kids in the chat",
                        "Someone asked my kid where they live", "The chat is not safe for children, please moderate"],
        "multiplayer_servers": ["Servers lag with many players", "Can't find or join my friends",
                                "Disconnected from the lobby", "Online mode is broken"],
    },
}
OFF_TOPIC_EXAMPLES = ["This review is about a different app", "Wrong app sorry", "Who won the football game",
                      "Does anyone know a good recipe", "What phone case should I buy"]
# "Love it, BUT it crashes": split mixed reviews and let the serious part win
CLAUSE_SPLIT = r"\b(?:but|however|although|aber|doch|jedoch|mas|porém|só que)\b|אבל|אך"
CRASH_WORDS = (r"crash|freez|closes|shuts down|won.t (open|start|launch)|stürzt|absturz|friert|schließt sich|"
               r"fecha sozinho|fecha toda|\btrava\b|não abre|קורס|קריסה|נסגר|לא נפתח")
PROGRESS_WORDS = r"progress|save|fortschritt|spielstand|progresso|התקדמות|השלבים"
LOSS_WORDS = r"lost|gone|wiped|deleted|reset|weg|gelöscht|verloren|perdi|apagou|sumiu|נמחק|מחק|נעלם|איבדתי"
SEVERE_CATEGORIES = {"crash_stability", "progress_account_loss", "payment_purchase", "chat_safety",
                     "performance_battery", "ads"}
PROTECTED_FLAGS = {"child_safety", "payment", "legal", "privacy", "store_policy"}
NEGATIVE_CATEGORIES = {"crash_stability", "progress_account_loss", "payment_purchase", "chat_safety"}

# ---------------------------------------------------------------------------
# Risk keywords (en / de / pt / he). Over-sensitive on purpose.
# ---------------------------------------------------------------------------
RISK = {
    "child_safety": [
        r"\b(my|our) (son|daughter|kid|child|children)\b.*\b(message|messag|chat|stranger|adult|address|live|lives)",
        r"\b(stranger|adult|grown.?up|creep|predator|groom)", r"\bnot safe for (kids|children)",
        r"\b(kids|children|minors)\b.*\b(messag|inappropriat|contact)", r"\binappropriat",
        r"\b(sohn|tochter|kind|kinder)\b.*\b(nachricht|chat|fremd|erwachsen|wohnt)", r"\b(fremde[rn]?|erwachsene[rn]?)\b",
        r"\b(filh[oa]|criança)\b.*\b(mensag|chat|estranh|adulto|mora)", r"\b(estranh[oa]|adulto)\b",
        r"(הבן|הבת|הילד|ילדים|לילדים).*(הודעות|צ'אט|זר|מבוגר|גר|גרה)", r"\b(זר|מבוגר)\b", r"לא בטוח לילדים",
        # sexual content / harassment: always a safety case in a game with young players
        r"\b(sexual|sexually|harass\w*|nude|naked|grooming|molest\w*|pedo\w*)\b",
        r"(sexuell|belästig|nackt|pädophil)", r"(assédio|assediad|pedófil|\bnudes?\b)",
        r"(מיני|מינית|הטרד|עירום|פדופיל|תמונות לא צנועות|נגיעות)",
    ],
    "payment": [
        # an act of paying + money symbols; NOT generic "costs money" (that is pricing, not a payment problem)
        r"\b(paid(?! only)|charged|purchased?|bought|refund|billing)\b", r"\b(bezahlt|abgebucht|gekauft|erstattung)",
        r"\b(paguei|cobrad|cobrou|cobraram|comprei|reembolso)", r"(שילמתי|חויבתי|קניתי|הכסף ירד|החזר כספי|רכישה)",
        r"[$€₪]|R\$",
    ],
    "legal": [r"\b(lawyer|sue|lawsuit|police|court|fraud|consumer protection)\b", r"\b(anwalt|polizei|betrug|klage|verbraucherschutz)",
              r"\b(advogado|polícia|processo|golpe|fraude|procon)", r"(עורך דין|תביעה|משטרה|הונאה|הגנת הצרכן)"],
    "privacy": [r"\b(personal data|my data|this data|privacy|gdpr|tracking me|tracking my)\b",
                r"\b(location|contacts)\b.*\b(kid|child|son|daughter)|\b(kid|child)\S*\b.*\b(location|contacts)\b",
                r"\b(datenschutz|meine daten|standort|kontakte)", r"\b(meus dados|privacidade|localização|contatos)",
                r"(פרטיות|המידע שלי|הנתונים שלי|מיקום|אנשי הקשר)"],
    # things that can get the app flagged by Google Play: ads policy, families policy, deceptive behavior
    "store_policy": [r"\b(casino|gambling|betting|adult content|sexual|nsfw)\b.*\bads?\b|\bads?\b.*\b(casino|gambling|betting|adult content|sexual)",
                     r"\b(installed|downloads?|downloaded) (another|other|an?) app", r"fake close|can.t close the ad|without asking",
                     r"(report|reporting|melde|denunciar|מדווח).{0,20}(google|play)", r"against google|google.s rules",
                     r"glücksspiel|cassino|apostas|הימורים|קזינו|תוכן למבוגרים", r"installiert|instalou outro|התקינה אפליקציה"],
    "high_value": [r"\bvip\b", r"\b(spent|spend|gastei|ausgegeben|gesteckt|הוצאתי)\b",
                   r"\b\d+ (years|jahren|anos)\b", r"(שנים|כבר \d+ שנ)", r"(\$|€|₪|R\$ ?)\s?\d{3,}|\d{3,}\s?(€|\$)"],
}
SPAM = [r"https?://|www\.|\.[a-z]{2,}\b(?<!\.\.)", r"\b(free|gratis|grátis) (coins|gems|münzen|moedas)",
        r"(מטבעות בחינם)", r"\b(hack|generator|unlimited)\b"]


# ---------------------------------------------------------------------------
# A. Noise
# ---------------------------------------------------------------------------
def letters(text):
    return [c for c in text if unicodedata.category(c).startswith("L")]


def noise_reason(text):
    t = text.strip()
    if not letters(t):
        return "emoji_or_symbols"
    if any(re.search(p, t.lower()) for p in SPAM):
        return "spam"
    words = re.findall(r"\w+", t)
    if len(words) <= 2 and len(t) < 15:
        return "too_short"
    low = t.lower()
    if len(set(low.split())) == 1 and len(low.split()) > 1:     # "lol lol lol"
        return "gibberish"
    letter_str = "".join(letters(low))
    if 5 <= len(letter_str) <= 15 and len(set(letter_str)) / len(letter_str) < 0.35:   # "aaaaaaa", "jjjjjj"
        return "gibberish"
    if re.fullmatch(r"[a-z]{5,}", letter_str) and not re.search(r"[aeiouy]", letter_str):   # "qwrtz"
        return "gibberish"
    if re.fullmatch(r"(asdf\w*|qwert\w*|zxcv\w*)", low):
        return "gibberish"
    return None


# ---------------------------------------------------------------------------
# B. Risk flags
# ---------------------------------------------------------------------------
def risk_flags(text, first_rating, rating):
    low = text.lower()
    flags = [name for name, pats in RISK.items() if any(re.search(p, low) for p in pats)]
    if first_rating is not None and first_rating >= 4 and rating <= 2:
        flags.append("rating_drop")        # was happy, came back angry
    return flags


# ---------------------------------------------------------------------------
# C. Embeddings + category
# ---------------------------------------------------------------------------
class Embedder:
    def __init__(self, name=MODEL_NAME):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(name)

    def encode(self, texts):
        return self.model.encode(texts, batch_size=128, show_progress_bar=True, normalize_embeddings=True)


def build_prototypes(embedder, family):
    cats = {**TAXONOMY["shared"], **TAXONOMY.get(family, {})}
    names, centroids = [], []
    for name, examples in cats.items():
        v = embedder.encode(examples).mean(axis=0)
        names.append(name)
        centroids.append(v / np.linalg.norm(v))
    off = embedder.encode(OFF_TOPIC_EXAMPLES).mean(axis=0)
    return names, np.vstack(centroids), off / np.linalg.norm(off)


def run(embedder=None):
    embedder = embedder or Embedder()
    con = sqlite3.connect(DB)
    rows = con.execute("""SELECT r.review_id, r.text, r.star_rating, r.first_star_rating, a.genre_family
                          FROM reviews r JOIN apps a USING(package_name) ORDER BY r.first_seen_at""").fetchall()

    results, keep_idx = {}, []
    for i, (rid, text, rating, first, family) in enumerate(rows):
        flags = risk_flags(text, first, rating)
        # SAFETY RULE: a review with a risk flag is never thrown away as noise
        reason = None if set(flags) & PROTECTED_FLAGS else noise_reason(text)
        results[rid] = {"is_noise": int(reason is not None), "noise_reason": reason, "category": None,
                        "category_score": None, "secondary_category": None, "flags": flags,
                        "rating_mismatch": 0}
        if reason is None:
            keep_idx.append(i)

    print(f"Embedding {len(keep_idx):,} reviews (noise removed: {len(rows) - len(keep_idx):,})")
    vecs = embedder.encode([rows[i][1] for i in keep_idx])

    # mixed reviews: embed each clause separately too
    clauses = {}
    for i in keep_idx:
        raw = re.split(CLAUSE_SPLIT, rows[i][1], flags=re.IGNORECASE)
        parts = [p.strip(" ,.!") for p in raw if p and len(p.strip(" ,.!")) > 3]
        if len(raw) > 1 and parts:
            clauses[i] = parts
    flat = [p for parts in clauses.values() for p in parts]
    print(f"Mixed reviews split into clauses: {len(clauses):,}")
    clause_vecs = iter(embedder.encode(flat)) if flat else iter([])

    protos = {fam: build_prototypes(embedder, fam) for fam in {r[4] for r in rows}}
    for vec, i in zip(vecs, keep_idx):
        rid, text, rating, first, family = rows[i]
        names, centroids, off = protos[family]
        sims = centroids @ vec
        order = np.argsort(-sims)
        best, second = int(order[0]), int(order[1])
        res = results[rid]
        part_vecs = [next(clause_vecs) for _ in clauses.get(i, [])]
        if float(off @ vec) > sims[best] and not set(res["flags"]) & PROTECTED_FLAGS:
            res.update(is_noise=1, noise_reason="off_topic", category=None)
            continue
        cat, score = (names[best], float(sims[best])) if sims[best] >= MIN_SCORE else ("other", float(sims[best]))
        # severity wins: if any clause is clearly about a serious problem, that is the category
        for pv in part_vecs:
            ps = centroids @ pv
            pb = int(np.argmax(ps))
            if names[pb] in SEVERE_CATEGORIES and ps[pb] >= MIN_SCORE and cat not in SEVERE_CATEGORIES:
                cat, score = names[pb], float(ps[pb])
        # safety net for hidden bugs: explicit crash words beat praise/other, in any language
        low = text.lower()
        if cat in ("praise", "other", "content_repetitive", "feature_request", "performance_battery") \
                and re.search(CRASH_WORDS, low):
            cat = "crash_stability"
        # same idea for lost progress: data loss is too serious to leave to "closest example"
        if re.search(PROGRESS_WORDS, low) and re.search(LOSS_WORDS, low):
            cat = "progress_account_loss"
        res["category"], res["category_score"] = cat, round(score, 3)
        # second category when the review is genuinely about two things
        res["secondary_category"] = names[second] if (sims[second] >= MIN_SCORE and sims[best] - sims[second] < 0.05
                                                      and names[second] != cat) else None
        res["rating_mismatch"] = int(rating >= 4 and cat in NEGATIVE_CATEGORIES)

    con.execute("DROP TABLE IF EXISTS review_analysis")
    con.execute("""CREATE TABLE review_analysis (review_id TEXT PRIMARY KEY, is_noise INTEGER, noise_reason TEXT,
                   category TEXT, category_score REAL, secondary_category TEXT, flags TEXT,
                   rating_mismatch INTEGER)""")
    con.executemany("INSERT INTO review_analysis VALUES (?,?,?,?,?,?,?,?)",
                    [(rid, r["is_noise"], r["noise_reason"], r["category"], r["category_score"],
                      r["secondary_category"], ",".join(r["flags"]), r["rating_mismatch"]) for rid, r in results.items()])
    con.commit()

    ids = [rows[i][0] for i in keep_idx]
    np.save("embeddings.npy", vecs.astype(np.float32))
    json.dump(ids, open("embedding_ids.json", "w"))
    report(con)
    con.close()


# ---------------------------------------------------------------------------
def report(con):
    q = lambda s: con.execute(s).fetchall()
    total = q("SELECT COUNT(*) FROM review_analysis")[0][0]
    print(f"\n=== {total:,} reviews ===")
    print("\nNoise (counted, not clustered):")
    for reason, n in q("SELECT noise_reason, COUNT(*) FROM review_analysis WHERE is_noise=1 GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {reason:18s} {n:,}")
    print("\nCategories by app (% of that app's non-noise reviews):")
    data = q("""SELECT a.app_name, ra.category, COUNT(*) FROM review_analysis ra JOIN reviews r USING(review_id)
                JOIN apps a USING(package_name) WHERE ra.is_noise=0 GROUP BY 1, 2""")
    apps = sorted({d[0] for d in data})
    for app in apps:
        tot = sum(n for a, _, n in data if a == app)
        top = sorted([(n, c) for a, c, n in data if a == app], reverse=True)[:6]
        print(f"  {app:13s} " + "  ".join(f"{c} {n / tot:.0%}" for n, c in top))
    print("\nRisk flags:")
    for flag in ["child_safety", "payment", "legal", "privacy", "high_value", "rating_drop"]:
        n = q(f"SELECT COUNT(*) FROM review_analysis WHERE ',' || flags || ',' LIKE '%,{flag},%'")[0][0]
        print(f"  {flag:14s} {n:,}")
    print(f"\nRating mismatch (4-5 stars, text reports a serious problem): "
          f"{q('SELECT COUNT(*) FROM review_analysis WHERE rating_mismatch=1')[0][0]}")


if __name__ == "__main__":
    run()
