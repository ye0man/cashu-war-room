"""Main entry point: fetch data and generate the Cashu War Room report."""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import (
    MINT_SNAPSHOT_DIR,
    REPOS,
    REPORT_DIR,
    default_start_date,
    github_token,
    today,
)
from data_store import DataStore
from github_client import GitHubClient
from mint_tracker import (
    fetch_mint_directory,
    load_snapshot,
    save_snapshot,
    summarize_changes,
)
from report_generator import load_dataframes, load_repo_snapshots, render_report, save_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the Cashu War Room report")
    parser.add_argument("--date", type=str, help="Report date as YYYY-MM-DD (default today)")
    parser.add_argument("--sample", action="store_true", help="Generate sample data and report instead of fetching GitHub")
    parser.add_argument("--no-fetch", action="store_true", help="Skip GitHub fetch; use existing local data")
    parser.add_argument("--force-refresh", action="store_true", help="Refetch all data instead of incremental")
    return parser.parse_args()


def _parse_date(date_str: str | None) -> datetime:
    if date_str:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return today()


def _generate_sample_data(store: DataStore, end: datetime) -> None:
    """Create plausible sample data so the report can be tested without a token."""
    random.seed(42)
    start = default_start_date(end)
    contributors = ["alice", "bob", "carol", "dave", "eve", "frank"]
    for repo in REPOS:
        commits = []
        for _ in range(random.randint(50, 200)):
            ts = start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))
            author = random.choice(contributors)
            commits.append({
                "id": f"{repo.full_name}-{_}",
                "repo": repo.full_name,
                "sha": f"sha{random.randint(100000,999999)}",
                "message": f"Sample commit by {author}",
                "authored_at": ts.isoformat(),
                "committed_at": ts.isoformat(),
                "author_login": author,
                "committer_login": author,
                "additions": random.randint(1, 500),
                "deletions": random.randint(0, 200),
                "changed_files": random.randint(1, 10),
                "url": f"https://github.com/{repo.full_name}/commit/sample",
            })
        store.append_records(repo.full_name, "commits", commits, id_field="id")

        issues = []
        for i in range(random.randint(10, 40)):
            created = start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))
            closed = created + timedelta(days=random.randint(0, 30)) if random.random() < 0.6 else None
            author = random.choice(contributors)
            issues.append({
                "id": f"issue-{repo.full_name}-{i}",
                "repo": repo.full_name,
                "number": i,
                "title": f"Sample issue {i} in {repo.name}",
                "state": "CLOSED" if closed else "OPEN",
                "created_at": created.isoformat(),
                "closed_at": closed.isoformat() if closed else None,
                "updated_at": (closed or created).isoformat(),
                "author_login": author,
                "assignee_logins": random.sample(contributors, k=random.randint(0, 2)),
                "labels": random.sample(["bug", "enhancement", "NUT", "help wanted"], k=random.randint(0, 2)),
                "comments_count": random.randint(0, 5),
                "url": f"https://github.com/{repo.full_name}/issues/{i}",
            })
        store.append_records(repo.full_name, "issues", issues, id_field="id")

        prs = []
        for i in range(random.randint(10, 40)):
            created = start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))
            merged = created + timedelta(days=random.randint(0, 14)) if random.random() < 0.5 else None
            author = random.choice(contributors)
            prs.append({
                "id": f"pr-{repo.full_name}-{i}",
                "repo": repo.full_name,
                "number": i,
                "title": f"Sample PR {i} in {repo.name}",
                "state": "MERGED" if merged else ("CLOSED" if random.random() < 0.3 else "OPEN"),
                "created_at": created.isoformat(),
                "merged_at": merged.isoformat() if merged else None,
                "closed_at": None,
                "updated_at": (merged or created).isoformat(),
                "author_login": author,
                "merged_by_login": random.choice(contributors) if merged else None,
                "assignee_logins": random.sample(contributors, k=random.randint(0, 2)),
                "labels": random.sample(["bug", "enhancement", "NUT"], k=random.randint(0, 2)),
                "additions": random.randint(1, 300),
                "deletions": random.randint(0, 100),
                "changed_files": random.randint(1, 8),
                "comments_count": random.randint(0, 8),
                "reviews_count": random.randint(0, 4),
                "review_threads_count": random.randint(0, 3),
                "url": f"https://github.com/{repo.full_name}/pull/{i}",
            })
        store.append_records(repo.full_name, "pull_requests", prs, id_field="id")

        reviews = []
        for i in range(random.randint(5, 20)):
            ts = start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))
            reviews.append({
                "id": f"review-{repo.full_name}-{i}",
                "repo": repo.full_name,
                "pr_number": random.randint(1, len(prs)),
                "author_login": random.choice(contributors),
                "state": random.choice(["APPROVED", "COMMENTED", "CHANGES_REQUESTED"]),
                "submitted_at": ts.isoformat(),
                "url": f"https://github.com/{repo.full_name}/pull/{i}#review",
            })
        store.append_records(repo.full_name, "pr_reviews", reviews, id_field="id")

        issue_comments = []
        for i in range(random.randint(5, 30)):
            ts = start + timedelta(seconds=random.randint(0, int((end - start).total_seconds())))
            issue_comments.append({
                "id": f"ic-{repo.full_name}-{i}",
                "repo": repo.full_name,
                "issue_number": random.randint(1, len(issues)),
                "author_login": random.choice(contributors),
                "created_at": ts.isoformat(),
                "updated_at": ts.isoformat(),
                "body": "Sample comment",
                "url": f"https://github.com/{repo.full_name}/issues/{i}#issuecomment",
            })
        store.append_records(repo.full_name, "issue_comments", issue_comments, id_field="id")

        releases = []
        for i in range(random.randint(0, 4)):
            ts = start + timedelta(days=random.randint(0, 365))
            releases.append({
                "id": f"rel-{repo.full_name}-{i}",
                "repo": repo.full_name,
                "tag_name": f"v0.{random.randint(1,20)}.{random.randint(0,5)}",
                "name": f"Release {i}",
                "published_at": ts.isoformat(),
                "author_login": random.choice(contributors),
                "prerelease": random.random() < 0.2,
                "url": f"https://github.com/{repo.full_name}/releases/tag/v0.0.{i}",
            })
        store.append_records(repo.full_name, "releases", releases, id_field="id")

        snapshot = {
            "id": repo.full_name,
            "repo": repo.full_name,
            "snapshot_at": end.isoformat(),
            "stars": random.randint(20, 600),
            "forks": random.randint(5, 150),
            "open_issues": len(issues) // 2,
            "watchers": random.randint(10, 300),
            "pushed_at": end.isoformat(),
            "default_branch": "main",
        }
        store.append_records(repo.full_name, "repo_snapshots", [snapshot], id_field="id")

    logger.info("Generated sample data for %d repos", len(REPOS))


def _fetch_github_data(client: GitHubClient, store: DataStore, end: datetime, force: bool) -> None:
    start = default_start_date(end)
    for repo in REPOS:
        logger.info("Fetching %s", repo.full_name)
        try:
            # Commits
            since_commits = start if force else (store.get_max_datetime(repo.full_name, "commits", "committed_at") or start)
            commits = client.fetch_commits(repo.owner, repo.name, since=since_commits)
            store.append_records(repo.full_name, "commits", commits, id_field="id")
            logger.info("  commits: %d", len(commits))

            # Issues and PRs updated since last run
            since_issues = start if force else (store.get_max_datetime(repo.full_name, "issues", "updated_at") or start)
            issues = client.fetch_issues(repo.owner, repo.name, since=since_issues)
            store.append_records(repo.full_name, "issues", issues, id_field="id")
            logger.info("  issues: %d", len(issues))

            since_prs = start if force else (store.get_max_datetime(repo.full_name, "pull_requests", "updated_at") or start)
            prs = client.fetch_pull_requests(repo.owner, repo.name, since=since_prs)
            store.append_records(repo.full_name, "pull_requests", prs, id_field="id")
            logger.info("  pull requests: %d", len(prs))

            # Issue comments
            since_ic = start if force else (store.get_max_datetime(repo.full_name, "issue_comments", "created_at") or start)
            issue_comments = client.fetch_issue_comments(repo.owner, repo.name, since=since_ic)
            store.append_records(repo.full_name, "issue_comments", issue_comments, id_field="id")
            logger.info("  issue comments: %d", len(issue_comments))

            # Releases
            releases = client.fetch_releases(repo.owner, repo.name, since=start)
            store.append_records(repo.full_name, "releases", releases, id_field="id")
            logger.info("  releases: %d", len(releases))

            # PR reviews and review comments for PRs touched in this run
            for pr in prs:
                pr_number = pr["number"]
                reviews = client.fetch_pr_reviews(repo.owner, repo.name, pr_number)
                store.append_records(repo.full_name, "pr_reviews", reviews, id_field="id")
                review_comments = client.fetch_pr_review_comments(repo.owner, repo.name, pr_number, since=start)
                store.append_records(repo.full_name, "pr_review_comments", review_comments, id_field="id")

            # Snapshot
            snapshot = client.fetch_repo_snapshot(repo.owner, repo.name)
            store.append_records(repo.full_name, "repo_snapshots", [snapshot], id_field="id")
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", repo.full_name, exc, exc_info=True)
            logger.error("If this is a permission error, make sure GH_TOKEN can read %s.", repo.full_name)


def _fetch_mint_updates() -> list[str]:
    current = fetch_mint_directory()
    save_snapshot(current)
    all_paths = sorted(MINT_SNAPSHOT_DIR.glob("*_mints.json"), reverse=True)
    # After saving, the first path is today's snapshot; compare against the previous one.
    previous_path = all_paths[1] if len(all_paths) > 1 else None
    if not previous_path:
        return ["No previous mint snapshot available for comparison."]
    previous = load_snapshot(previous_path)
    changes = summarize_changes(current, previous)
    if not changes:
        return ["No updates to any tracked mints since the last snapshot."]
    return changes


def _fetch_nut_items(client: GitHubClient) -> list[dict[str, Any]]:
    nuts_repo = next(r for r in REPOS if r.name == "nuts")
    try:
        return client.search_nut_items(nuts_repo.owner, nuts_repo.name, state="open")
    except Exception as exc:
        logger.error("Failed to fetch NUT items: %s", exc)
        return []


def main() -> int:
    args = _parse_args()
    report_date = _parse_date(args.date)
    store = DataStore()

    if args.sample:
        _generate_sample_data(store, report_date)
        mint_changes = ["Sample mint update: no real data fetched."]
        nut_items = []
    else:
        token = github_token()
        if not token and not args.no_fetch:
            logger.error("GH_TOKEN is required to fetch GitHub data. Set it as an environment variable.")
            logger.error("Run with --sample to test report generation without a token.")
            return 1

        client = GitHubClient(token)
        if not args.no_fetch:
            _fetch_github_data(client, store, report_date, args.force_refresh)
        else:
            logger.info("Skipping GitHub fetch; using existing local data.")

        mint_changes = _fetch_mint_updates()
        nut_items = _fetch_nut_items(client) if client.token else []

    frames = load_dataframes(store)
    snapshot_df = load_repo_snapshots(store)
    html = render_report(report_date, frames, snapshot_df, mint_changes, nut_items)
    report_path = save_report(html, report_date)
    logger.info("Report saved to %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
