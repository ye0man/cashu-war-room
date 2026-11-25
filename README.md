# Cashu War Room

A daily activity report for the Cashu ecosystem. It pulls GitHub and Forgejo data
for a curated set of Cashu repositories, diffs the Cashu Mint Directory against its
previous snapshot, and renders a static HTML dashboard.

## Daily schedule

The report runs automatically via GitHub Actions **every day at 06:00 UTC**
(`cron: '0 6 * * *'` in `.github/workflows/daily-report.yml`). It can also be
triggered manually from the Actions tab with `workflow_dispatch`.

Each run:

1. Fetches commits, issues, pull requests, reviews, comments, releases, and repo
   snapshots for the tracked repositories.
2. Saves a snapshot of the Cashu Mint Directory and computes changes since the
   previous snapshot.
3. Writes `reports/cashu-war-room-YYYY-MM-DD.html` plus `reports/index.html`.
4. Commits the updated `data/` and `reports/` directories back to `main`.

`.github/workflows/pages.yml` publishes `reports/` to GitHub Pages. It triggers on
`workflow_run` completion of the daily report rather than only on push, because
commits pushed with the repository's built-in `GITHUB_TOKEN` do not start new
workflow runs (GitHub suppresses them to prevent recursion). A push-only trigger
therefore leaves the published site frozen at the last manual deploy.

## What's in the report

- **Momentum** - new issues, opened/merged PRs, closed issues, releases, and
  PRs reviewed over the last 7 days, with daily activity and
  per-repo/per-contributor breakdowns. A release table shows each repo's current
  stable release and days since it was published.
- **Contributors** - per-repo and ecosystem-wide leaderboard with commits, lines,
  issues, PRs, and review activity.
- **Assigned open work** - open issues and PRs assigned to each person, plus
  pending review requests awaiting their response.
- **Issue / PR health** - open item counts, average age, oldest open items, and
  average time to merge or close.
- **Ecosystem snapshot** - stars, forks, and open issues per repository, with
  day-over-day deltas.
- **Mint updates** - additions, removals, and field changes in the
  [Cashu Mint Directory](https://ye0man.github.io/cashu-mint-directory/).

Reports older than 30 days are pruned automatically.

## Tracked repositories

Defined in `config.py`: `cdk`, `coco`, `cashu-ts`, `cashu.me`, `nuts`, `nutshell`,
`Numo`, `wallet`, `orchard`, `cdk-payment-processors`, `docs.cashu.space`,
`cashu-skill`, `Sovran`, `CashuSwift`, `macadamia`, `pecan`, and
`internet-mint-finder`.

Most repos are on GitHub. Repositories hosted elsewhere are declared with a
`forge` and `host` in `config.py` (currently Forgejo at `git.cashu.dev`), and are
read through the same-shaped client in `forgejo_client.py`.

## Running locally

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export GH_TOKEN=ghp_...            # Windows: $env:GH_TOKEN = "ghp_..."
python report.py
```

The token needs read access to the tracked repositories. For the scheduled
workflow, configure it as a repository secret or variable named `GH_TOKEN`.

Forgejo-hosted repositories are read anonymously unless `FORGEJO_TOKEN` (or
`GITEA_TOKEN`) is set; set it if the instance requires auth or rate limits bite.

Useful flags:

```bash
python report.py --sample          # generate a report from sample data, no token needed
python report.py --no-fetch        # use existing local data
python report.py --force-refresh   # refetch the full lookback window
python report.py --date 2026-10-01 # generate a report for a specific date
```

## Layout

```
config.py              tracked repos, paths, lookback and retention settings
github_client.py       GitHub GraphQL/REST client
forgejo_client.py      Forgejo/Gitea REST client (same record shapes)
data_store.py          incremental JSONL storage
mint_tracker.py        mint directory snapshots and diffs
report_generator.py    metrics computation and HTML rendering
report.py              entry point
templates/report.html  report template
data/                  raw GitHub extracts and mint snapshots
reports/               generated HTML reports
```

## Notes

- Raw data is stored incrementally as JSONL under `data/raw/<owner>/<repo>/`.
- `GH_TOKEN` is read from the environment and is never written to the repo.
- The report is plain static HTML with no build step or external runtime.
