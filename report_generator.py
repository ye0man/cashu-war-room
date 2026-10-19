"""Compute metrics and render the static HTML report."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import (
    REPORT_DIR,
    REPOS,
    TEMPLATE_DIR,
    today,
)
from data_store import DataStore

logger = logging.getLogger(__name__)


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


def _login_of(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _repo_short(repo: Any) -> str:
    return repo.split("/", 1)[1] if isinstance(repo, str) and "/" in repo else (repo or "")


def _window_records(df: pd.DataFrame, date_col: str) -> list[dict[str, Any]]:
    """Records sorted newest-first, with display helpers attached."""
    if df.empty:
        return []
    ordered = df.sort_values(date_col, ascending=False).copy()
    dates = pd.to_datetime(ordered[date_col], utc=True, errors="coerce")
    ordered["date_str"] = dates.dt.strftime("%Y-%m-%d")
    ordered["repo_short"] = ordered["repo"].map(_repo_short) if "repo" in ordered.columns else ""
    return ordered.to_dict("records")


def _blank_contributor() -> dict[str, Any]:
    return {
        "commits": 0, "additions": 0, "deletions": 0, "days_active": set(),
        "issues_opened": 0, "prs_opened": 0, "prs_merged": 0,
        "assigned_issues": [], "assigned_prs": [],
    }


def _finish_contributors(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for login, data in rows.items():
        if (
            login == "unknown"
            and data["commits"] == 0
            and data["issues_opened"] == 0
            and data["prs_opened"] == 0
            and not data["assigned_issues"]
            and not data["assigned_prs"]
        ):
            continue
        out.append({
            "login": login,
            "commits": data["commits"],
            "additions": data["additions"],
            "deletions": data["deletions"],
            "days_active": len(data["days_active"]),
            "issues_opened": data["issues_opened"],
            "prs_opened": data["prs_opened"],
            "prs_merged": data["prs_merged"],
            "assigned_issues": sorted(data["assigned_issues"], key=lambda r: str(r.get("created_at") or ""), reverse=True),
            "assigned_prs": sorted(data["assigned_prs"], key=lambda r: str(r.get("created_at") or ""), reverse=True),
            "has_assigned": bool(data["assigned_issues"] or data["assigned_prs"]),
            "is_active": bool(data["commits"] or data["issues_opened"] or data["prs_opened"] or data["prs_merged"]),
        })
    out.sort(key=lambda c: (-c["commits"], -c["prs_opened"], -c["issues_opened"], c["login"]))
    return out


def _assigned_item(row: Any, relation: str | None = None) -> dict[str, Any]:
    created = row.get("created_at")
    rec = row.to_dict()
    rec["date_str"] = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else ""
    rec["repo_short"] = _repo_short(rec.get("repo"))
    if relation:
        rec["relation"] = relation
    return rec


def compute_momentum(frames: dict[str, pd.DataFrame], end: datetime) -> dict[str, Any]:
    end = pd.Timestamp(end).tz_convert("UTC")
    start = end - timedelta(days=7)

    issues = frames.get("issues", pd.DataFrame())
    prs = frames.get("pull_requests", pd.DataFrame())
    releases = frames.get("releases", pd.DataFrame())
    commits = frames.get("commits", pd.DataFrame())

    new_issues = issues[(issues["created_at"] >= start) & (issues["created_at"] <= end)] if not issues.empty else issues
    opened_prs = prs[(prs["created_at"] >= start) & (prs["created_at"] <= end)] if not prs.empty else prs
    merged_prs = prs[(prs["merged_at"] >= start) & (prs["merged_at"] <= end)] if not prs.empty else prs
    closed_issues = issues[(issues["closed_at"] >= start) & (issues["closed_at"] <= end)] if not issues.empty else issues
    new_releases = releases[(releases["published_at"] >= start) & (releases["published_at"] <= end)] if not releases.empty else releases

    # Contributor rows scoped globally and per repo.
    global_rows: dict[str, dict[str, Any]] = defaultdict(_blank_contributor)
    repo_rows: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(_blank_contributor))

    def _bump(login: str | None, repo: str | None, field: str, amount: int = 1) -> None:
        user = _login_of(login) or "unknown"
        global_rows[user][field] += amount
        if repo:
            repo_rows[repo][user][field] += amount

    if not commits.empty:
        recent_commits = commits[(commits["committed_at"] >= start) & (commits["committed_at"] <= end)]
        for _, row in recent_commits.iterrows():
            user = _login_of(row.get("author_login")) or "unknown"
            repo = row.get("repo")
            day = row["committed_at"].strftime("%Y-%m-%d")
            additions = int(row.get("additions", 0) or 0)
            deletions = int(row.get("deletions", 0) or 0)
            for table in (global_rows[user], repo_rows[repo][user] if repo else None):
                if table is None:
                    continue
                table["commits"] += 1
                table["additions"] += additions
                table["deletions"] += deletions
                table["days_active"].add(day)

    for _, row in new_issues.iterrows():
        _bump(row.get("author_login"), row.get("repo"), "issues_opened")

    for _, row in opened_prs.iterrows():
        _bump(row.get("author_login"), row.get("repo"), "prs_opened")

    for _, row in merged_prs.iterrows():
        _bump(row.get("author_login"), row.get("repo"), "prs_merged")

    # Assigned open items (current state)
    open_issues = issues[issues["state"] == "OPEN"] if not issues.empty else issues
    open_prs = prs[prs["state"] == "OPEN"] if not prs.empty else prs

    for _, row in open_issues.iterrows():
        item = _assigned_item(row)
        for assignee in _as_list(row.get("assignee_logins")):
            user = _login_of(assignee)
            if not user:
                continue
            global_rows[user]["assigned_issues"].append(item)
            repo_rows[row.get("repo")][user]["assigned_issues"].append(item)

    for _, row in open_prs.iterrows():
        repo = row.get("repo")
        assignees = [a for a in (_login_of(x) for x in _as_list(row.get("assignee_logins"))) if a]
        author = _login_of(row.get("author_login"))
        if assignees:
            # Assignee takes priority: the PR is only listed under its assignees.
            for assignee in assignees:
                item = _assigned_item(row, "assigned")
                global_rows[assignee]["assigned_prs"].append(item)
                repo_rows[repo][assignee]["assigned_prs"].append(item)
        elif author:
            # Unassigned PR: fall back to the author ("authored").
            item = _assigned_item(row, "authored")
            global_rows[author]["assigned_prs"].append(item)
            repo_rows[repo][author]["assigned_prs"].append(item)

    # By repo
    by_repo: dict[str, dict[str, Any]] = {}
    for repo in [r.full_name for r in REPOS]:
        scoped = {}
        for col_frame, sort_col, name in (
            (new_issues, "created_at", "new_issues"),
            (opened_prs, "created_at", "opened_prs"),
            (merged_prs, "merged_at", "merged_prs"),
            (closed_issues, "closed_at", "closed_issues"),
            (new_releases, "published_at", "releases"),
        ):
            subset = col_frame[col_frame["repo"] == repo] if not col_frame.empty else col_frame
            scoped[name] = _window_records(subset, sort_col)
        scoped["contributors"] = _finish_contributors(repo_rows.get(repo, {}))
        by_repo[repo] = scoped

    combined = {
        "new_issues": _window_records(new_issues, "created_at"),
        "opened_prs": _window_records(opened_prs, "created_at"),
        "merged_prs": _window_records(merged_prs, "merged_at"),
        "closed_issues": _window_records(closed_issues, "closed_at"),
        "releases": _window_records(new_releases, "published_at"),
    }

    by_contributor = _finish_contributors(global_rows)

    return {
        "start": start,
        "end": end,
        "by_repo": by_repo,
        "combined": combined,
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
        oldest_issues = open_issues.sort_values("age_days", ascending=False).head(10).copy()
        oldest_issues["date_str"] = pd.to_datetime(oldest_issues["created_at"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d")
        oldest_issues["repo_short"] = oldest_issues["repo"].map(_repo_short)
        result["oldest_open_issues"] = oldest_issues.to_dict("records")

    if not open_prs.empty:
        open_prs["age_days"] = age_days(open_prs["created_at"])
        oldest_prs = open_prs.sort_values("age_days", ascending=False).head(10).copy()
        oldest_prs["date_str"] = pd.to_datetime(oldest_prs["created_at"], utc=True, errors="coerce").dt.strftime("%Y-%m-%d")
        oldest_prs["repo_short"] = oldest_prs["repo"].map(_repo_short)
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
            "short": _repo_short(repo),
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
    template = env.get_template("report.html")

    context = {
        "report_date": report_date,
        "start_date": start,
        "repos": [{"full_name": r.full_name, "display": r.display} for r in REPOS],
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
