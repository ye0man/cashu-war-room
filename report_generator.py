"""Compute metrics and render the static HTML report."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from plotly.subplots import make_subplots

from config import (
    DATA_DIR,
    RAW_DIR,
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


def _dt(value: str | datetime | None) -> pd.Timestamp | pd.NaTType:
    if pd.isna(value):
        return pd.NaT
    if isinstance(value, datetime):
        return pd.Timestamp(value)
    if isinstance(value, str):
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            return pd.Timestamp(value)
        except ValueError:
            return pd.NaT
    return pd.NaT


def _month_bucket(ts: pd.Timestamp | None) -> str:
    if pd.isna(ts):
        return "unknown"
    return ts.strftime("%Y-%m")


def _link(text: str, url: str) -> str:
    return Markup(f'<a href="{url}" target="_blank" rel="noopener">{text}</a>')


def _figure_to_json(fig: go.Figure) -> str:
    return fig.to_json(engine="json")


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
# Historic contributor analysis
# ---------------------------------------------------------------------------


def compute_historic_metrics(
    frames: dict[str, pd.DataFrame],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Return monthly aggregated metrics by repo and contributor."""
    start = pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp(end).tz_convert("UTC")

    rows: list[dict[str, Any]] = []

    commits = frames.get("commits", pd.DataFrame())
    if not commits.empty:
        commits = commits[(commits["committed_at"] >= start) & (commits["committed_at"] <= end)].copy()
        commits["month"] = commits["committed_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in commits.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "commits": len(group),
                "additions": int(group["additions"].sum()),
                "deletions": int(group["deletions"].sum()),
                "lines_changed": int(group["additions"].sum() + group["deletions"].sum()),
            })

    prs = frames.get("pull_requests", pd.DataFrame())
    if not prs.empty:
        prs = prs[(prs["created_at"] >= start) & (prs["created_at"] <= end)].copy()
        prs["month"] = prs["created_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in prs.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "prs_opened": len(group),
            })
        merged = prs[prs["merged_at"].notna()].copy()
        merged["month"] = merged["merged_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in merged.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "prs_merged": len(group),
            })

    issues = frames.get("issues", pd.DataFrame())
    if not issues.empty:
        issues = issues[(issues["created_at"] >= start) & (issues["created_at"] <= end)].copy()
        issues["month"] = issues["created_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in issues.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "issues_opened": len(group),
            })
        closed = issues[issues["closed_at"].notna()].copy()
        closed["month"] = closed["closed_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in closed.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "issues_closed": len(group),
            })

    comments = frames.get("issue_comments", pd.DataFrame())
    if not comments.empty:
        comments = comments[(comments["created_at"] >= start) & (comments["created_at"] <= end)].copy()
        comments["month"] = comments["created_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in comments.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "issue_comments": len(group),
            })

    reviews = frames.get("pr_reviews", pd.DataFrame())
    if not reviews.empty:
        reviews = reviews[(reviews["created_at"] >= start) & (reviews["created_at"] <= end)].copy()
        reviews["month"] = reviews["created_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in reviews.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "reviews": len(group),
            })

    review_comments = frames.get("pr_review_comments", pd.DataFrame())
    if not review_comments.empty:
        review_comments = review_comments[(review_comments["created_at"] >= start) & (review_comments["created_at"] <= end)].copy()
        review_comments["month"] = review_comments["created_at"].dt.to_period("M").dt.to_timestamp()
        for (month, repo, author), group in review_comments.groupby(["month", "repo", "author_login"]):
            rows.append({
                "month": month,
                "repo": repo,
                "contributor": author or "unknown",
                "review_comments": len(group),
            })

    if not rows:
        return pd.DataFrame()

    metrics = pd.DataFrame(rows)
    metric_cols = ["commits", "additions", "deletions", "lines_changed", "prs_opened", "prs_merged",
                   "issues_opened", "issues_closed", "issue_comments", "reviews", "review_comments"]
    for col in metric_cols:
        if col not in metrics.columns:
            metrics[col] = 0
    metrics[metric_cols] = metrics[metric_cols].fillna(0).astype(int)
    return metrics


def make_historic_charts(metrics: pd.DataFrame) -> dict[str, str]:
    charts: dict[str, str] = {}
    if metrics.empty:
        return charts

    # Overall monthly activity by repo (commits)
    overall = metrics.groupby(["month", "repo"])["commits"].sum().unstack(fill_value=0).reset_index()
    fig = go.Figure()
    for repo in overall.columns[1:]:
        fig.add_trace(go.Scatter(x=overall["month"].tolist(), y=overall[repo].tolist(), mode="lines+markers", name=repo, stackgroup="one"))
    fig.update_layout(
        title="Monthly Commits by Repo",
        xaxis_title="Month",
        yaxis_title="Commits",
        hovermode="x unified",
        template="plotly_white",
    )
    charts["commits_by_repo"] = _figure_to_json(fig)

    # Lines changed by repo
    lines = metrics.groupby(["month", "repo"])["lines_changed"].sum().unstack(fill_value=0).reset_index()
    fig2 = go.Figure()
    for repo in lines.columns[1:]:
        fig2.add_trace(go.Bar(x=lines["month"].tolist(), y=lines[repo].tolist(), name=repo))
    fig2.update_layout(
        title="Monthly Lines Changed by Repo",
        xaxis_title="Month",
        yaxis_title="Lines",
        barmode="stack",
        template="plotly_white",
    )
    charts["lines_by_repo"] = _figure_to_json(fig2)

    # PRs / issues by repo
    pr_issue = metrics.groupby("month")[["prs_opened", "prs_merged", "issues_opened", "issues_closed"]].sum().reset_index()
    fig3 = go.Figure()
    for col in ["prs_opened", "prs_merged", "issues_opened", "issues_closed"]:
        fig3.add_trace(go.Scatter(x=pr_issue["month"].tolist(), y=pr_issue[col].tolist(), mode="lines+markers", name=col))
    fig3.update_layout(
        title="Monthly PR & Issue Activity",
        xaxis_title="Month",
        yaxis_title="Count",
        hovermode="x unified",
        template="plotly_white",
    )
    charts["pr_issue_activity"] = _figure_to_json(fig3)

    # Top contributors by commits (last 12 months)
    top = metrics.groupby("contributor")["commits"].sum().sort_values(ascending=False).head(15)
    fig4 = go.Figure(go.Bar(x=top.index.tolist(), y=top.tolist()))
    fig4.update_layout(title="Top Contributors by Commits (Selected Period)", xaxis_title="Contributor", yaxis_title="Commits", template="plotly_white")
    charts["top_contributors"] = _figure_to_json(fig4)

    return charts


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
    result: dict[str, Any] = {"chart_json": None, "latest": {}}
    if snapshot_df.empty:
        return result

    pivot = snapshot_df.pivot_table(index="snapshot_at", columns="repo", values="stars", aggfunc="last").sort_index()
    fig = go.Figure()
    for repo in pivot.columns:
        fig.add_trace(go.Scatter(x=pivot.index.tolist(), y=pivot[repo].tolist(), mode="lines+markers", name=repo))
    fig.update_layout(
        title="Star Growth by Repo",
        xaxis_title="Date",
        yaxis_title="Stars",
        hovermode="x unified",
        template="plotly_white",
    )
    result["chart_json"] = _figure_to_json(fig)

    latest = snapshot_df.sort_values("snapshot_at").groupby("repo").last()
    for repo, row in latest.iterrows():
        result["latest"][repo] = {
            "stars": int(row.get("stars", 0)),
            "forks": int(row.get("forks", 0)),
            "open_issues": int(row.get("open_issues", 0)),
        }
    return result


# ---------------------------------------------------------------------------
# New contributors
# ---------------------------------------------------------------------------


def compute_new_contributors(frames: dict[str, pd.DataFrame], end: datetime, window_days: int = 90) -> list[dict[str, Any]]:
    end = pd.Timestamp(end).tz_convert("UTC")
    start_window = end - timedelta(days=window_days)
    start_historical = end - timedelta(days=365)

    commits = frames.get("commits", pd.DataFrame())
    prs = frames.get("pull_requests", pd.DataFrame())
    issues = frames.get("issues", pd.DataFrame())

    def first_event_dates(df: pd.DataFrame, date_col: str, actor_col: str, label: str) -> dict[str, pd.Timestamp]:
        if df.empty:
            return {}
        df = df[(df[date_col] >= start_historical) & (df[date_col] <= end)]
        subset = df[[date_col, actor_col]].dropna()
        subset = subset[subset[actor_col] != "unknown"]
        firsts = subset.groupby(actor_col)[date_col].min().to_dict()
        return firsts

    first_commit = first_event_dates(commits, "committed_at", "author_login", "commit")
    first_pr = first_event_dates(prs, "created_at", "author_login", "pr")
    first_issue = first_event_dates(issues, "created_at", "author_login", "issue")

    all_people = set(first_commit) | set(first_pr) | set(first_issue)
    newcomers = []
    for person in all_people:
        first = min(
            [first_commit.get(person), first_pr.get(person), first_issue.get(person)],
            key=lambda x: x if x is not None else pd.Timestamp.max,
        )
        if first and first >= start_window:
            newcomers.append({"login": person, "first_seen": first})

    newcomers.sort(key=lambda x: x["first_seen"], reverse=True)
    return newcomers


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(
    report_date: datetime,
    frames: dict[str, pd.DataFrame],
    snapshot_df: pd.DataFrame,
    mint_changes: list[str],
    nut_items: list[dict[str, Any]],
) -> str:
    start = default_start_date(report_date)
    historic_metrics = compute_historic_metrics(frames, start, report_date)
    historic_charts = make_historic_charts(historic_metrics)
    momentum = compute_momentum(frames, report_date)
    aging = compute_aging(frames, report_date)
    growth = compute_growth(snapshot_df)
    new_contributors = compute_new_contributors(frames, report_date, window_days=90)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["link"] = _link
    template = env.get_template("report.html")

    # Top contributors table
    top_contributors = (
        historic_metrics.groupby("contributor")[
            ["commits", "additions", "deletions", "prs_opened", "prs_merged", "issues_opened", "issues_closed", "reviews", "review_comments", "issue_comments"]
        ]
        .sum()
        .sort_values("commits", ascending=False)
        .head(30)
        .reset_index()
        .fillna(0)
        .to_dict("records")
        if not historic_metrics.empty
        else []
    )

    context = {
        "report_date": report_date,
        "start_date": start,
        "repos": [r.full_name for r in REPOS],
        "historic_charts": historic_charts,
        "top_contributors": top_contributors,
        "momentum": momentum,
        "aging": aging,
        "growth": growth,
        "new_contributors": new_contributors,
        "mint_changes": mint_changes,
        "nut_items": nut_items,
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
    return end - timedelta(days=365)
