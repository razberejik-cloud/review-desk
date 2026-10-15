# Review Morning Desk

A proof of concept for turning a stream of Google Play reviews (many apps, many languages) into:

- **issues a product person can act on** (release regressions, spikes, new themes, chronic topics),
- **a personal-reply queue** for reviews a person must answer (safety, legal, money, lost progress, valuable players),
- **automatic replies** for the safe rest, each checked against Google Play's review policy,
- **urgent pages** to the app's product manager and Legal when a review can hurt the app in the store.

Everything runs on free tools: Python, SQLite, a local multilingual embedding model, GitHub Actions and GitHub Pages.

## Try it live

1. Open the dashboard (GitHub Pages link in the repo's About box).
2. Click **Submit a test review** (or Issues → New issue → *Simulate a Play review*).
3. Write a serious review, for example *"An adult player keeps sending private messages to my 9 year old"*.
4. Within about a minute: the issue gets a reply showing the lane and who was paged, and the dashboard's **Live intake** panel turns red with the urgent alert.

The dashboard also replays the 60 mock days morning by morning (▶ or the date buttons).

## How it works

| Step | Script | What it does |
| --- | --- | --- |
| 1 | `pipeline/generate_play_reviews.py` | Mock Google Play API stream: 17,623 reviews, 4 apps, 3 genres, 4 languages, 60 days, planted problems |
| 2 | `pipeline/collector.py` | Daily pull of the API's 7-day window → SQLite, skips repeats, keeps edit history |
| 3 | `pipeline/classify.py` | Noise filter, risk rules (4 languages), topic classification with a multilingual model |
| 4 | `pipeline/cluster.py` | Theme discovery per genre × topic; new themes are born from the stream with a date |
| 5 | `pipeline/trends.py` | Shares vs each app's normal → release / spike / new-theme / growing / chronic signals, merged into issues |
| 6 | `pipeline/triage.py` | One lane per review, draft replies, 9-point policy guard, incident replies edited when the fix ships |
| 6b | `pipeline/urgent.py` | Hourly rules-only check that pages owners (child safety 1 h, store policy 4 h, legal 4 h, privacy 24 h) |
| 7 | `pipeline/export_dashboard.py` + `build_site.py` | Data for the dashboard in `site/` |
| live | `live/intake.py` + `.github/workflows/intake.yml` | One new review → rules → lane → page → `site/live.json` → redeploy |
| check | `pipeline/evaluate.py` | Scores everything against `data/ground_truth.csv`, which the pipeline never reads |

Run the full pipeline from scratch: open `notebooks/review_desk_pipeline.ipynb` in Google Colab and choose Run all (about 6-9 minutes, free CPU). The finished database is also in `data/reviews.db`.

Owner contacts for pages are in `config/owners.json` (fake addresses). Put GitHub usernames in `github_pm` to @mention a real person as the product manager; empty means the repo owner.

## Google Play constraints this respects

- Replies never ask for a higher rating, offer rewards, or contain codes, links (other than support) or emails. ([policy](https://support.google.com/googleplay/android-developer/answer/9898684))
- Replies are plain text, 350 characters at most; 2,000 replies per app per day. ([Reply to Reviews API](https://developers.google.com/android-publisher/reply-to-reviews))
- The API returns only the last 7 days and no country, so the system stores its own history.
