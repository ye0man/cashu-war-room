"""Compute metrics and render the static HTML report."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from config import (
    REPORT_DIR,
    REPOS,
    TEMPLATE_DIR,
    today,
)
from data_store import DataStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _link(text: str, url: str) -> str:
    return Markup(f'<a href="{url}" target="_blank" rel="noopener">{text}</a>')


def _user_link(login: str | None) -> str:
    if not login:
        return ""
    return Markup(
        f'<a href="https://github.com/{login}" target="_blank" rel="noopener">@{login}</a>'
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_dataframes(store: DataStore | None = None) -> dict[str, pd.DataFrame]:
    store = store or DataStore()
    frames: dict[str, pd.DataFrame] = {}
    entities = ["commits", "issues", "issue_comments", "pull_requests", "pr_reviews", "pr_review_comments", "releases"]
    for entity in entities:
        records: list[dict[str, Any]] = []
        for repo in REPOS:
            records.extend(store.load_records(repo.full_name, entity))
        df = pd.DataFrame(records)
        if not df.empty:
            if entity == "commits":
                df["committed_at"] = pd.to_datetime(df["committed_at"], utc=True, errors="coerce")
                df["authored_at"] = pd.to_datetime(df["authored_at"], utc=True, errors="coerce")
            elif entity in {"issues", "pull_requests"}:
                df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
                df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
                if "merged_at" in df.columns:
                    df["merged_at"] = pd.to_datetime(df["merged_at"], utc=True, errors="coerce")
                if "updated_at" in df.columns:
                    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True, errors="coerce")
            elif entity in {"issue_comments", "pr_review_comments"}:
                df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
            elif entity == "pr_reviews":
                df["created_at"] = pd.to_datetime(df["submitted_at"], utc=True, errors="coerce")
                df["submitted_at"] = pd.to_datetime(df["submitted_at"], utc=True, errors="coerce")
            elif entity == "releases":
                df["published_at"] = pd.to_datetime(df["published_at"], utc=True, errors="coerce")

        frames[entity] = df
    return frames


def load_repo_snapshots(store: DataStore | None = None) -> pd.DataFrame:
    store = store or DataStore()
    records: list[dict[str, Any]] = []
    for repo in REPOS:
        records.extend(store.load_records(repo.full_name, "repo_snapshots"))
    df = pd.DataFrame(records)
    if not df.empty:
        df["snapshot_at"] = pd.to_datetime(df["snapshot_at"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Momentum (last 7 days)
# ---------------------------------------------------------------------------


def compute_momentum(frames: dict[str, pd.DataFrame], end: datetime) -> dict[str, Any]:
    end = pd.Timestamp(end).tz_convert("UTC")
    start = end - timedelta(days=7)

    issues = frames.get("issues", pd.DataFrame())
    prs = frames.get("pull_requests", pd.DataFrame())
    releases = frames.get("releases", pd.DataFrame())

    new_issues = issues[(issues["created_at"] >= start) & (issues["created_at"] <= end)] if not issues.empty else issues
    opened_prs = prs[(prs["created_at"] >= start) & (prs["created_at"] <= end)] if not prs.empty else prs
    merged_prs = prs[(prs["merged_at"] >= start) & (prs["merged_at"] <= end)] if not prs.empty else prs
    closed_issues = issues[(issues["closed_at"] >= start) & (issues["closed_at"] <= end)] if not issues.empty else issues
    new_releases = releases[(releases["published_at"] >= start) & (releases["published_at"] <= end)] if not releases.empty else releases

    # By repo
    by_repo: dict[str, dict[str, Any]] = {}
    for repo in [r.full_name for r in REPOS]:
        by_repo[repo] = {
            "new_issues": new_issues[new_issues["repo"] == repo].sort_values("created_at", ascending=False).to_dict("records") if not new_issues.empty else [],
            "opened_prs": opened_prs[opened_prs["repo"] == repo].sort_values("created_at", ascending=False).to_dict("records") if not opened_prs.empty else [],
            "merged_prs": merged_prs[merged_prs["repo"] == repo].sort_values("merged_at", ascending=False).to_dict("records") if not merged_prs.empty else [],
            "closed_issues": closed_issues[closed_issues["repo"] == repo].sort_values("closed_at", ascending=False).to_dict("records") if not closed_issues.empty else [],
            "releases": new_releases[new_releases["repo"] == repo].sort_values("published_at", ascending=False).to_dict("records") if not new_releases.empty else [],
        }

    # By contributor
    contributor_rows: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "commits": 0, "additions": 0, "deletions": 0, "days_active": set(),
        "issues_opened": 0, "prs_opened": 0, "prs_merged": 0,
        "assigned_issues": [], "assigned_prs": [],
    })

    commits = frames.get("commits", pd.DataFrame())
    if not commits.empty:
        recent_commits = commits[(commits["committed_at"] >= start) & (commits["committed_at"] <= end)]
        for _, row in recent_commits.iterrows():
            login = row.get("author_login") or "unknown"
            contributor_rows[login]["commits"] += 1
            contributor_rows[login]["additions"] += int(row.get("additions", 0))
            contributor_rows[login]["deletions"] += int(row.get("deletions", 0))
            contributor_rows[login]["days_active"].add(row["committed_at"].strftime("%Y-%m-%d"))

    for _, row in new_issues.iterrows():
        login = row.get("author_login")
        if login:
            contributor_rows[login]["issues_opened"] += 1

    for _, row in opened_prs.iterrows():
        login = row.get("author_login")
        if login:
            contributor_rows[login]["prs_opened"] += 1

    for _, row in merged_prs.iterrows():
        login = row.get("author_login")
        if login:
            contributor_rows[login]["prs_merged"] += 1

    # Assigned open items (current state)
    open_issues = issues[issues["state"] == "OPEN"] if not issues.empty else issues
    open_prs = prs[prs["state"] == "OPEN"] if not prs.empty else prs
    for _, row in open_issues.iterrows():
        for assignee in row.get("assignee_logins", []):
            contributor_rows[assignee]["assigned_issues"].append(row.to_dict())
    for _, row in open_prs.iterrows():
        for assignee in row.get("assignee_logins", []):
            contributor_rows[assignee]["assigned_prs"].append(row.to_dict())

    by_contributor = []
    for login, data in sorted(contributor_rows.items(), key=lambda x: x[1]["commits"], reverse=True):
        if login == "unknown" and data["commits"] == 0 and data["issues_opened"] == 0 and data["prs_opened"] == 0:
            continue
        by_contributor.append({
            "login": login,
            "commits": data["commits"],
            "additions": data["additions"],
            "deletions": data["deletions"],
            "days_active": len(data["days_active"]),
            "issues_opened": data["issues_opened"],
            "prs_opened": data["prs_opened"],
            "prs_merged": data["prs_merged"],
            "assigned_issues": data["assigned_issues"],
            "assigned_prs": data["assigned_prs"],
        })

    return {
        "start": start,
        "end": end,
        "by_repo": by_repo,
        "by_contributor": by_contributor,
        "summary": {
            "new_issues": len(new_issues),
            "opened_prs": len(opened_prs),
            "merged_prs": len(merged_prs),
            "closed_issues": len(closed_issues),
            "releases": len(new_releases),
            "active_contributors": len([c for c in by_contributor if c["commits"] or c["issues_opened"] or c["prs_opened"]]),
        },
    }


# ---------------------------------------------------------------------------
# Issue / PR aging
# ---------------------------------------------------------------------------


def compute_aging(frames: dict[str, pd.DataFrame], end: datetime) -> dict[str, Any]:
    end = pd.Timestamp(end).tz_convert("UTC")
    issues = frames.get("issues", pd.DataFrame())
    prs = frames.get("pull_requests", pd.DataFrame())

    open_issues = issues[issues["state"] == "OPEN"].copy() if not issues.empty else issues
    open_prs = prs[prs["state"] == "OPEN"].copy() if not prs.empty else prs

    def age_days(created_at: pd.Series) -> pd.Series:
        return (end - pd.to_datetime(created_at, utc=True)).dt.days

    result = {
        "open_issues_count": len(open_issues),
        "open_issues_avg_age": round(age_days(open_issues["created_at"]).mean(), 1) if not open_issues.empty else 0,
        "open_prs_count": len(open_prs),
        "open_prs_avg_age": round(age_days(open_prs["created_at"]).mean(), 1) if not open_prs.empty else 0,
        "oldest_open_issues": [],
        "oldest_open_prs": [],
        "avg_time_to_merge_days": None,
        "avg_time_to_close_issue_days": None,
    }

    if not open_issues.empty:
        open_issues["age_days"] = age_days(open_issues["created_at"])
        oldest_issues = open_issues.sort_values("age_days", ascending=False).head(10)
        result["oldest_open_issues"] = oldest_issues.to_dict("records")

    if not open_prs.empty:
        open_prs["age_days"] = age_days(open_prs["created_at"])
        oldest_prs = open_prs.sort_values("age_days", ascending=False).head(10)
        result["oldest_open_prs"] = oldest_prs.to_dict("records")

    merged = prs[prs["merged_at"].notna()].copy() if not prs.empty else prs
    if not merged.empty:
        merged["time_to_merge"] = (merged["merged_at"] - merged["created_at"]).dt.days
        result["avg_time_to_merge_days"] = round(merged["time_to_merge"].mean(), 1)

    closed_issues = issues[issues["closed_at"].notna()].copy() if not issues.empty else issues
    if not closed_issues.empty:
        closed_issues["time_to_close"] = (closed_issues["closed_at"] - closed_issues["created_at"]).dt.days
        result["avg_time_to_close_issue_days"] = round(closed_issues["time_to_close"].mean(), 1)

    return result


# ---------------------------------------------------------------------------
# Growth (stars / forks)
# ---------------------------------------------------------------------------


def compute_growth(snapshot_df: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {"latest": {}}
    if snapshot_df.empty:
        return result

    latest = snapshot_df.sort_values("snapshot_at").groupby("repo").last()
    for repo, row in latest.iterrows():
        result["latest"][repo] = {
            "stars": int(row.get("stars", 0)),
            "forks": int(row.get("forks", 0)),
            "open_issues": int(row.get("open_issues", 0)),
        }
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(
    report_date: datetime,
    frames: dict[str, pd.DataFrame],
    snapshot_df: pd.DataFrame,
    mint_changes: list[str],
) -> str:
    start = default_start_date(report_date)
    momentum = compute_momentum(frames, report_date)
    aging = compute_aging(frames, report_date)
    growth = compute_growth(snapshot_df)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["link"] = _link
    env.filters["user_link"] = _user_link
    template = env.get_template("report.html")

    context = {
        "report_date": report_date,
        "start_date": start,
        "repos": [r.full_name for r in REPOS],
        "momentum": momentum,
        "aging": aging,
        "growth": growth,
        "mint_changes": mint_changes,
    }
    return template.render(context)


def save_report(html: str, date: datetime | None = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date = date or today()
    dated = REPORT_DIR / f"cashu-war-room-{date.strftime('%Y-%m-%d')}.html"
    latest = REPORT_DIR / "index.html"
    dated.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")
    return dated


def default_start_date(end: datetime) -> datetime:
    return end - timedelta(days=7)
