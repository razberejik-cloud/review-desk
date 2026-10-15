"""
Step 4 - Discover themes and keep learning from the stream
==========================================================

Step 3 gave every review a FIXED category (crash, ads, feature_request...).
Step 4 finds the SPECIFIC themes inside each category, per genre, without us
telling it what to look for. "feature_request" in PeleGame splits into e.g.
"avatar customization" vs "pets"; "crash_stability" in Merge Meadow becomes
"crashes on launch after update".

How it learns from the stream (instead of re-reading everything every day):

  1. BASELINE  - cluster the first 30 days (HDBSCAN on the step-3 embeddings,
                 separately for every genre x category).
  2. DAILY     - each new review joins the closest existing theme, if it is
                 close enough (each theme learns its own "how close is close"
                 from its members).
  3. NEW THEME - reviews that fit no theme wait in an "unassigned pool".
                 When enough similar ones pile up, the pool is clustered and a
                 NEW THEME is born, with the date it appeared. That date is the
                 "something new started happening" alert.

Naming is free too: each theme gets its top keywords (across all languages)
and its most typical review as a quote. An LLM could write nicer names later;
it is optional, not required.

Output (in reviews.db):
  themes          one row per theme: genre, category, keywords, quote, born_on, size
  review_themes   review_id -> theme_id (or NULL = still unassigned), assigned_on

Run after classify.py:  python cluster.py
"""

import json
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

import numpy as np
from sklearn.cluster import HDBSCAN
from sklearn.feature_extraction.text import TfidfVectorizer

DB = "reviews.db"
BASELINE_DAYS = 30
MIN_THEME = 12            # smallest group we call a theme
NEW_THEME_WINDOW = 7      # pool reviews older than this don't count toward a new theme
MIN_SIM_FLOOR = 0.45      # never attach a review below this similarity
MERGE_SIM = 0.80          # two themes this similar are the same theme (e.g. same complaint in 2 languages)

STOP = set("""the a an and or but to of in on for is it this that i my me we you be are was with so at as
very please just all can not no app game der die das und ist ich es nicht ein eine zu mit mein meine sehr
o a os as e de do da que em um uma para com não eu meu minha muito jogo את של על זה לא אני עם גם כל הוא
היא יותר מדי בבקשה honestly hi devs ehrlich gesagt hallo sinceramente oi האמת היי""".split())


# ---------------------------------------------------------------------------
def load():
    con = sqlite3.connect(DB)
    vecs = np.load("embeddings.npy")
    ids = json.load(open("embedding_ids.json"))
    pos = {rid: i for i, rid in enumerate(ids)}
    rows = con.execute("""
        SELECT r.review_id, substr(r.last_modified_at, 1, 10), r.text, r.reviewer_language,
               r.app_version_name, r.device, a.genre, a.app_name, ra.category
        FROM reviews r JOIN apps a USING(package_name) JOIN review_analysis ra USING(review_id)
        WHERE ra.is_noise = 0 ORDER BY r.last_modified_at""").fetchall()
    reviews = [dict(zip(["id", "day", "text", "lang", "version", "device", "genre", "app", "category"], r))
               for r in rows if r[0] in pos]
    for r in reviews:
        r["vec"] = vecs[pos[r["id"]]]
    return con, reviews


def unit(v):
    return v / (np.linalg.norm(v) + 1e-9)


class Theme:
    _next = 0

    def __init__(self, genre, category, members, born_on):
        Theme._next += 1
        self.id = f"T{Theme._next:03d}"
        self.genre, self.category, self.born_on = genre, category, born_on
        self.members = list(members)
        self.refresh()

    def refresh(self):
        m = np.vstack([r["vec"] for r in self.members])
        self.centroid = unit(m.mean(axis=0))
        sims = m @ self.centroid
        # "how close is close" for this theme: the 5th percentile of its own members
        self.threshold = max(MIN_SIM_FLOOR, float(np.percentile(sims, 5)))

    def add(self, r):
        self.members.append(r)
        if len(self.members) % 25 == 0:      # drift slowly with the stream
            self.refresh()


def discover(group, genre, category, born_on):
    """HDBSCAN on a set of reviews -> list of new Themes (+ leftovers)."""
    if len(group) < MIN_THEME:
        return [], group
    X = np.vstack([r["vec"] for r in group])
    labels = HDBSCAN(min_cluster_size=MIN_THEME, min_samples=5, metric="euclidean").fit_predict(X)
    themes = [Theme(genre, category, [r for r, l in zip(group, labels) if l == k], born_on)
              for k in sorted(set(labels) - {-1})]
    leftovers = [r for r, l in zip(group, labels) if l == -1]
    return themes, leftovers


def merge_similar(themes):
    """Join themes of the same genre x category whose centres are almost identical.
    Keeps the earliest born_on, so a merged theme is not reported as 'new'."""
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(themes):
            for b in themes[i + 1:]:
                if a.genre == b.genre and a.category == b.category and float(a.centroid @ b.centroid) >= MERGE_SIM:
                    keep, drop = (a, b) if len(a.members) >= len(b.members) else (b, a)
                    keep.members += drop.members
                    keep.born_on = min(keep.born_on, drop.born_on)
                    keep.refresh()
                    themes.remove(drop)
                    merged = True
                    break
            if merged:
                break
    return themes


def name_themes(themes):
    """Keywords via c-TF-IDF (one 'document' per theme) + the most typical review."""
    docs = [" ".join(r["text"] for r in t.members) for t in themes]
    tok = lambda s: [w for w in re.findall(r"[^\W\d_]{3,}", s.lower()) if w not in STOP]
    tfidf = TfidfVectorizer(tokenizer=tok, lowercase=False, token_pattern=None, sublinear_tf=True)
    M = tfidf.fit_transform(docs)
    vocab = np.array(tfidf.get_feature_names_out())
    for i, t in enumerate(themes):
        row = M[i].toarray()[0]
        t.keywords = ", ".join(vocab[np.argsort(-row)[:6]])
        sims = np.vstack([r["vec"] for r in t.members]) @ t.centroid
        order = np.argsort(-sims)
        en = [t.members[j] for j in order[:20] if t.members[j]["lang"] == "en"]
        t.quote = (en[0] if en else t.members[order[0]])["text"]


# ---------------------------------------------------------------------------
def run():
    con, reviews = load()
    start = date.fromisoformat(reviews[0]["day"])
    cutoff = (start + timedelta(days=BASELINE_DAYS)).isoformat()
    themes, assigned, pool = [], {}, defaultdict(list)
    births = []

    # 1. BASELINE
    base = defaultdict(list)
    for r in reviews:
        if r["day"] < cutoff:
            base[(r["genre"], r["category"])].append(r)
    for (genre, cat), group in base.items():
        new, left = discover(group, genre, cat, born_on=reviews[0]["day"])
        themes += new
        for t in new:
            for r in t.members:
                assigned[r["id"]] = (t.id, r["day"])
        pool[(genre, cat)] += left
    raw = len(themes)
    themes = merge_similar(themes)
    for t in themes:
        for r in t.members:
            assigned[r["id"]] = (t.id, r["day"])
    print(f"Baseline ({BASELINE_DAYS} days): {raw} raw groups -> {len(themes)} themes after merging look-alikes")

    # 2 + 3. DAILY STREAM
    by_day = defaultdict(list)
    for r in reviews:
        if r["day"] >= cutoff:
            by_day[r["day"]].append(r)
    for day in sorted(by_day):
        for r in by_day[day]:
            cands = [t for t in themes if t.genre == r["genre"] and t.category == r["category"]]
            best = max(cands, key=lambda t: float(r["vec"] @ t.centroid), default=None)
            if best is not None and float(r["vec"] @ best.centroid) >= best.threshold:
                best.add(r)
                assigned[r["id"]] = (best.id, day)
            else:
                pool[(r["genre"], r["category"])].append(r)
        # try to form new themes from recent unassigned reviews
        window = (date.fromisoformat(day) - timedelta(days=NEW_THEME_WINDOW)).isoformat()
        for key, waiting in pool.items():
            recent = [r for r in waiting if r["day"] > window]
            if len(recent) < MIN_THEME:
                continue
            new, _ = discover(recent, key[0], key[1], born_on=day)
            new = merge_similar(new)
            for t in new:
                # a "new" group that looks like an existing theme just joins it
                twin = max((x for x in themes if x.genre == t.genre and x.category == t.category),
                           key=lambda x: float(x.centroid @ t.centroid), default=None)
                if twin is not None and float(twin.centroid @ t.centroid) >= MERGE_SIM:
                    for r in t.members:
                        twin.add(r)
                        assigned[r["id"]] = (twin.id, day)
                    continue
                themes.append(t)
                births.append(t)
                for r in t.members:
                    assigned[r["id"]] = (t.id, day)
            if new:
                taken = {r["id"] for t in new for r in t.members}
                pool[key] = [r for r in waiting if r["id"] not in taken]

    name_themes(themes)

    # save
    con.execute("DROP TABLE IF EXISTS themes")
    con.execute("DROP TABLE IF EXISTS review_themes")
    con.execute("""CREATE TABLE themes (theme_id TEXT PRIMARY KEY, genre TEXT, category TEXT, keywords TEXT,
                   quote TEXT, born_on TEXT, size INTEGER, languages TEXT, apps TEXT)""")
    con.execute("CREATE TABLE review_themes (review_id TEXT PRIMARY KEY, theme_id TEXT, assigned_on TEXT)")
    for t in themes:
        langs = Counter(r["lang"] for r in t.members)
        apps = Counter(r["app"] for r in t.members)
        con.execute("INSERT INTO themes VALUES (?,?,?,?,?,?,?,?,?)",
                    (t.id, t.genre, t.category, t.keywords, t.quote, t.born_on, len(t.members),
                     json.dumps(dict(langs)), json.dumps(dict(apps))))
    con.executemany("INSERT INTO review_themes VALUES (?,?,?)",
                    [(r["id"], *assigned.get(r["id"], (None, None))) for r in reviews])
    con.commit()

    # report
    n_assigned = sum(1 for r in reviews if r["id"] in assigned)
    print(f"Final: {len(themes)} themes, {n_assigned:,}/{len(reviews):,} reviews in a theme "
          f"({n_assigned / len(reviews):.0%}), rest still unassigned")
    print("\nNEW THEMES born from the stream (after the baseline):")
    for t in sorted(births, key=lambda t: t.born_on):
        print(f"  {t.born_on}  {t.id}  {t.genre:13s} {t.category:22s} n={len(t.members):<4} [{t.keywords}]")
        print(f"              \"{t.quote[:90]}\"")
    print("\nLargest themes:")
    for t in sorted(themes, key=lambda t: -len(t.members))[:12]:
        langs = Counter(r["lang"] for r in t.members)
        print(f"  {t.id} {t.genre:13s} {t.category:22s} n={len(t.members):<5} langs={dict(langs)}")
        print(f"        [{t.keywords}]  \"{t.quote[:80]}\"")
    con.close()


if __name__ == "__main__":
    run()
