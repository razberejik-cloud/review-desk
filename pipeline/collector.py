"""
Step 2 - Collector: Google Play -> SQLite, with history
=======================================================

What it does, once per day per app:
  1. Ask Google Play for "all reviews created or modified in the last 7 days"
  2. Save each review it hasn't seen
  3. If a review it HAS seen came back with a newer lastModified, the user
     edited it -> keep the old version in history and record the change
  4. Ignore exact repeats (the 7-day windows overlap, so most reviews come
     back up to 7 times - that's expected, not new data)
  5. Log every run (how many fetched / new / edited / repeats)

Why: the API forgets anything older than a week and never shows the previous
rating of an edited review. Only our own database can answer "this player was
5 stars and dropped to 1 after update 2.4.0".

In this POC, fetch_reviews() reads the fake stream from step 1. To go live you
swap that single function for fetch_reviews_live() (bottom of file).

Run:  python collector.py      -> creates reviews.db
"""

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB = "reviews.db"
API_DUMP = "data/play_api_pulls.jsonl"
APPS_CSV = "data/apps.csv"
WINDOW = timedelta(days=7)          # what the real API returns

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    package_name  TEXT PRIMARY KEY,
    app_name      TEXT,
    genre         TEXT,
    genre_family  TEXT,
    audience      TEXT
);

-- one row per review = its CURRENT state
CREATE TABLE IF NOT EXISTS reviews (
    review_id          TEXT PRIMARY KEY,
    package_name       TEXT REFERENCES apps(package_name),
    author_name        TEXT,
    text               TEXT,
    star_rating        INTEGER,
    reviewer_language  TEXT,
    app_version_name   TEXT,
    app_version_code   INTEGER,
    device             TEXT,
    device_name        TEXT,
    manufacturer       TEXT,
    ram_mb             INTEGER,
    android_os_version INTEGER,
    thumbs_up          INTEGER,
    first_seen_at      TEXT,    -- when the review was first written
    last_modified_at   TEXT,    -- latest edit
    first_star_rating  INTEGER, -- rating in the first version we saw
    times_edited       INTEGER DEFAULT 0,
    developer_reply    TEXT,    -- filled in later steps
    replied_at         TEXT
);

-- every version of every review we ever saw (the history the API doesn't keep)
CREATE TABLE IF NOT EXISTS review_versions (
    review_id         TEXT,
    last_modified_at  TEXT,
    star_rating       INTEGER,
    text              TEXT,
    app_version_name  TEXT,
    collected_at      TEXT,
    PRIMARY KEY (review_id, last_modified_at)
);

-- audit log: one row per (run, app)
CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_at        TEXT,
    package_name  TEXT,
    fetched       INTEGER,
    new_reviews   INTEGER,
    edited        INTEGER,
    repeats       INTEGER
);

CREATE INDEX IF NOT EXISTS ix_reviews_app_time ON reviews(package_name, first_seen_at);
CREATE INDEX IF NOT EXISTS ix_reviews_version ON reviews(package_name, app_version_name);

-- handy view: players who came back and changed their rating
CREATE VIEW IF NOT EXISTS rating_changes AS
SELECT r.review_id, a.app_name, r.first_star_rating, r.star_rating,
       r.star_rating - r.first_star_rating AS delta,
       r.app_version_name, r.last_modified_at, r.text
FROM reviews r JOIN apps a USING (package_name)
WHERE r.times_edited > 0 AND r.star_rating <> r.first_star_rating;
"""


# ---------------------------------------------------------------------------
# Source of reviews. POC: replay the fake stream with the real API's 7-day rule.
# ---------------------------------------------------------------------------
def _load_fake_stream():
    by_pkg = defaultdict(lambda: defaultdict(list))   # pkg -> reviewId -> [versions]
    with open(API_DUMP, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            uc = obj["comments"][0]["userComment"]
            obj["_ts"] = datetime.fromtimestamp(int(uc["lastModified"]["seconds"]), timezone.utc)
            by_pkg[obj["_packageName"]][obj["reviewId"]].append(obj)
    for revs in by_pkg.values():
        for versions in revs.values():
            versions.sort(key=lambda o: o["_ts"])
    return by_pkg


_STREAM = None


def fetch_reviews(package_name, as_of):
    """Behaves like reviews.list: returns the CURRENT state of every review
    created or modified in the 7 days before `as_of`."""
    global _STREAM
    if _STREAM is None:
        _STREAM = _load_fake_stream()
    out = []
    for versions in _STREAM[package_name].values():
        current = None
        for v in versions:
            if v["_ts"] <= as_of:
                current = v
        if current and current["_ts"] > as_of - WINDOW:
            out.append(current)
    return out


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
def to_row(obj):
    uc = obj["comments"][0]["userComment"]
    dm = uc.get("deviceMetadata", {})
    ts = datetime.fromtimestamp(int(uc["lastModified"]["seconds"]), timezone.utc).isoformat()
    return {
        "review_id": obj["reviewId"], "package_name": obj["_packageName"], "author_name": obj.get("authorName"),
        "text": uc["text"].strip(),               # API prefixes text with a tab
        "star_rating": uc["starRating"], "reviewer_language": uc.get("reviewerLanguage"),
        "app_version_name": uc.get("appVersionName"), "app_version_code": uc.get("appVersionCode"),
        "device": uc.get("device"), "device_name": dm.get("productName"), "manufacturer": dm.get("manufacturer"),
        "ram_mb": dm.get("ramMb"), "android_os_version": uc.get("androidOsVersion"),
        "thumbs_up": uc.get("thumbsUpCount", 0), "last_modified_at": ts,
    }


def collect(con, package_name, as_of):
    fetched = fetch_reviews(package_name, as_of)
    new = edited = repeats = 0
    now = as_of.isoformat()
    for obj in fetched:
        r = to_row(obj)
        existing = con.execute("SELECT last_modified_at FROM reviews WHERE review_id=?", (r["review_id"],)).fetchone()
        if existing is None:
            con.execute("""INSERT INTO reviews (review_id, package_name, author_name, text, star_rating,
                    reviewer_language, app_version_name, app_version_code, device, device_name, manufacturer,
                    ram_mb, android_os_version, thumbs_up, first_seen_at, last_modified_at, first_star_rating)
                    VALUES (:review_id, :package_name, :author_name, :text, :star_rating, :reviewer_language,
                    :app_version_name, :app_version_code, :device, :device_name, :manufacturer, :ram_mb,
                    :android_os_version, :thumbs_up, :last_modified_at, :last_modified_at, :star_rating)""", r)
            new += 1
        elif r["last_modified_at"] > existing[0]:
            con.execute("""UPDATE reviews SET text=:text, star_rating=:star_rating,
                    app_version_name=:app_version_name, app_version_code=:app_version_code,
                    thumbs_up=:thumbs_up, last_modified_at=:last_modified_at, times_edited=times_edited+1
                    WHERE review_id=:review_id""", r)
            edited += 1
        else:
            # same state as before; only refresh counters that change without an edit
            con.execute("UPDATE reviews SET thumbs_up=? WHERE review_id=?", (r["thumbs_up"], r["review_id"]))
            repeats += 1
            continue
        con.execute("""INSERT OR IGNORE INTO review_versions VALUES (?,?,?,?,?,?)""",
                    (r["review_id"], r["last_modified_at"], r["star_rating"], r["text"], r["app_version_name"], now))
    con.execute("INSERT INTO ingestion_runs VALUES (?,?,?,?,?,?)", (now, package_name, len(fetched), new, edited, repeats))
    return len(fetched), new, edited, repeats


def load_apps(con):
    with open(APPS_CSV, encoding="utf-8") as f:
        rows = [tuple(r.values()) for r in csv.DictReader(f)]
    con.executemany("INSERT OR REPLACE INTO apps VALUES (?,?,?,?,?)", rows)
    return [r[0] for r in rows]


def main(start=datetime(2026, 8, 2, 6, tzinfo=timezone.utc), days=60):
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    packages = load_apps(con)
    totals = defaultdict(int)
    for d in range(days):                       # in production: a scheduled job, once a day
        as_of = start + timedelta(days=d)
        for pkg in packages:
            f, n, e, r = collect(con, pkg, as_of)
            totals["fetched"] += f; totals["new"] += n; totals["edited"] += e; totals["repeats"] += r
        con.commit()

    print(f"Daily runs: {days} days x {len(packages)} apps")
    print(f"Fetched from API: {totals['fetched']:,}  ->  new: {totals['new']:,}  "
          f"edited: {totals['edited']}  repeats ignored: {totals['repeats']:,}")
    q = lambda sql: con.execute(sql).fetchall()
    print("\nReviews stored per app:")
    for row in q("SELECT a.app_name, COUNT(*) FROM reviews JOIN apps a USING(package_name) GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {row[0]:14s} {row[1]:,}")
    print("\nRating changes we could only see because we keep history:")
    for row in q("""SELECT app_name, first_star_rating || '->' || star_rating AS change, COUNT(*)
                    FROM rating_changes GROUP BY 1, 2 ORDER BY 3 DESC"""):
        print(f"  {row[0]:14s} {row[1]}  x{row[2]}")
    print("\nExample:")
    for row in q("SELECT app_name, first_star_rating, star_rating, app_version_name, text FROM rating_changes LIMIT 2"):
        print(f"  {row[0]}: {row[1]}★ -> {row[2]}★ on {row[3]}: \"{row[4]}\"")
    con.close()
    print(f"\nSaved {DB}")


# ---------------------------------------------------------------------------
# Going live: replace fetch_reviews with this (needs a service account with the
# "Reply to reviews" permission in Play Console).
# ---------------------------------------------------------------------------
def fetch_reviews_live(package_name, as_of=None):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json", scopes=["https://www.googleapis.com/auth/androidpublisher"])
    api = build("androidpublisher", "v3", credentials=creds).reviews()
    out, token = [], None
    while True:
        resp = api.list(packageName=package_name, maxResults=100, token=token).execute()
        for r in resp.get("reviews", []):
            r["_packageName"] = package_name
            out.append(r)
        token = resp.get("tokenPagination", {}).get("nextPageToken")
        if not token:
            return out


if __name__ == "__main__":
    import os
    if os.path.exists(DB):
        os.remove(DB)          # POC: rebuild from scratch each run
    main()
