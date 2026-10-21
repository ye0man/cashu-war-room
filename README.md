# Cashu War Room

A daily activity report for the Cashu ecosystem. It pulls GitHub data for a curated
set of Cashu repositories, diffs the Cashu Mint Directory against its previous
snapshot, and renders a static HTML dashboard.

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

## What's in the report

- **Momentum** - new issues, opened/merged PRs, closed issues, and releases over
  the last 7 days, with daily activity and per-repo/per-contributor breakdowns.
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
`cashu-skill`, `Sovran`, `CashuSwift`, `macadamia`, and `pecan`.

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
