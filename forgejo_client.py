"""Forgejo/Gitea API client with the same record shapes as GitHubClient."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import forgejo_token

logger = logging.getLogger(__name__)

FORGEJO_API_VERSION = "v1"
PAGE_LIMIT = 50

_PR_REF_RE = re.compile(r"\(#(\d+)\)")


class ForgejoClient:
    """Read-only client for a Forgejo instance's REST API.

    Records mirror github_client.GitHubClient field-for-field so the report
    generator does not need to know which forge produced a row.
    """

    def __init__(self, host: str, token: str | None = None) -> None:
        self.host = host
        self.base = f"https://{host}/api/{FORGEJO_API_VERSION}"
        self.token = token or forgejo_token()
        if not self.token:
            logger.info("No FORGEJO_TOKEN set; using anonymous read access to %s.", host)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if self.token:
            self.session.headers["Authorization"] = f"token {self.token}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _login(data: dict[str, Any] | None, *keys: str) -> str | None:
        current: Any = data
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current if isinstance(current, str) and current else None

    @staticmethod
    def _state(value: str | None) -> str:
        return (value or "").upper()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.base}{path}"
        last_error: Exception | None = None
        for attempt in range(3):
            resp = self.session.get(url, params=params)
            if resp.status_code in {502, 503, 504}:
                wait = 2 ** attempt
                logger.warning("Forgejo returned %d; retrying in %ds", resp.status_code, wait)
                time.sleep(wait)
                last_error = requests.HTTPError(f"{resp.status_code} from {url}", response=resp)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_error:
            raise last_error
        return None

    def _paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        stop_when: Any = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        params.setdefault("limit", PAGE_LIMIT)
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            params["page"] = page
            batch = self._get(path, params)
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if stop_when is not None and stop_when(batch):
                break
            if len(batch) < int(params["limit"]):
                break
            page += 1
        return results

    # ------------------------------------------------------------------
    # Fetchers
    # ------------------------------------------------------------------

    def fetch_commits(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        cutoff = since.astimezone(timezone.utc)
        commits = self._paginate(
            f"/repos/{owner}/{name}/commits",
            {"since": self._iso(since)},
            stop_when=lambda batch: _any_before(batch, cutoff, ("commit", "committer", "date")),
        )
        records: list[dict[str, Any]] = []
        for c in commits:
            commit = c.get("commit") or {}
            committed = _parse_ts(_dig(commit, "committer", "date"))
            if committed is not None and committed < cutoff:
                continue
            author_name = _actor_login(c.get("author"), _dig(commit, "author", "email"))
            committer_name = _actor_login(c.get("committer"), _dig(commit, "committer", "email"))
            message = commit.get("message") or ""
            stats = c.get("stats") or {}
            files = c.get("files") or []
            records.append({
                "id": c["sha"],
                "repo": f"{owner}/{name}",
                "sha": c["sha"],
                "message": message,
                "authored_at": _dig(commit, "author", "date"),
                "committed_at": _dig(commit, "committer", "date"),
                "author_login": author_name,
                "committer_login": committer_name,
                "additions": stats.get("additions", 0) or 0,
                "deletions": stats.get("deletions", 0) or 0,
                "changed_files": stats.get("total") or len(files),
                "associated_merged_pr_numbers": sorted({int(n) for n in _PR_REF_RE.findall(message)}),
                "url": c.get("html_url") or f"https://{self.host}/{owner}/{name}/commit/{c['sha']}",
            })
        return records

    def fetch_issues(
        self, owner: str, name: str, since: datetime | None = None,
        states: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        states = states or ["OPEN", "CLOSED"]
        params: dict[str, Any] = {
            "state": self._state_param(states),
            "type": "issues",
            "sort": "recentupdate",
            "direction": "desc",
        }
        if since is not None:
            params["since"] = self._iso(since)
        cutoff = since.astimezone(timezone.utc) if since else None
        issues = self._paginate(
            f"/repos/{owner}/{name}/issues",
            params,
            stop_when=(lambda batch: _any_before(batch, cutoff, ("updated_at",))) if cutoff else None,
        )
        records: list[dict[str, Any]] = []
        for issue in issues:
            updated = _parse_ts(issue.get("updated_at"))
            if cutoff is not None and updated is not None and updated < cutoff:
                continue
            records.append({
                "id": str(issue["id"]),
                "repo": f"{owner}/{name}",
                "number": issue["number"],
                "title": issue.get("title") or "",
                "state": self._state(issue.get("state")),
                "created_at": issue.get("created_at"),
                "closed_at": issue.get("closed_at"),
                "updated_at": issue.get("updated_at"),
                "author_login": self._login(issue.get("user"), "login"),
                "assignee_logins": [
                    a["login"] for a in (issue.get("assignees") or []) if a.get("login")
                ],
                "labels": [l["name"] for l in (issue.get("labels") or []) if l.get("name")],
                "comments_count": issue.get("comments", 0) or 0,
                "url": issue.get("html_url"),
            })
        return records

    def fetch_pull_requests(
        self, owner: str, name: str, since: datetime | None = None,
        states: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        states = states or ["OPEN", "CLOSED", "MERGED"]
        wanted = {s.upper() for s in states}
        # Gitea/Forgejo only know open/closed; merged PRs are closed with merged=true.
        state_param = "open" if wanted == {"OPEN"} else "all"
        cutoff = since.astimezone(timezone.utc) if since else None

        prs = self._paginate(
            f"/repos/{owner}/{name}/pulls",
            {"state": state_param, "sort": "recentupdate", "direction": "desc"},
            stop_when=(lambda batch: _any_before(batch, cutoff, ("updated_at",))) if cutoff else None,
        )
        records: list[dict[str, Any]] = []
        for pr in prs:
            merged = bool(pr.get("merged"))
            state = "MERGED" if merged else self._state(pr.get("state"))
            if wanted and state not in wanted:
                continue
            updated = _parse_ts(pr.get("updated_at"))
            if cutoff is not None and updated is not None and updated < cutoff:
                continue
            records.append({
                "id": str(pr["id"]),
                "repo": f"{owner}/{name}",
                "number": pr["number"],
                "title": pr.get("title") or "",
                "state": state,
                "created_at": pr.get("created_at"),
                "merged_at": pr.get("merged_at"),
                "closed_at": pr.get("closed_at"),
                "updated_at": pr.get("updated_at"),
                "author_login": self._login(pr.get("user"), "login"),
                "merged_by_login": self._login(pr.get("merged_by"), "login"),
                "assignee_logins": [
                    a["login"] for a in (pr.get("assignees") or []) if a.get("login")
                ],
                "review_request_logins": [
                    r["login"] for r in (pr.get("requested_reviewers") or []) if r.get("login")
                ],
                "review_request_teams": [
                    t["name"] for t in (pr.get("requested_reviewers_teams") or []) if t.get("name")
                ],
                "labels": [l["name"] for l in (pr.get("labels") or []) if l.get("name")],
                "additions": pr.get("additions", 0) or 0,
                "deletions": pr.get("deletions", 0) or 0,
                "changed_files": pr.get("changed_files", 0) or 0,
                "comments_count": pr.get("comments", 0) or 0,
                "reviews_count": 0,
                "review_threads_count": pr.get("review_comments", 0) or 0,
                "url": pr.get("html_url"),
            })
        return records

    def fetch_issue_comments(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        comments = self._paginate(
            f"/repos/{owner}/{name}/issues/comments", {"since": self._iso(since)}
        )
        return [
            {
                "id": str(c["id"]),
                "repo": f"{owner}/{name}",
                "issue_number": _issue_number(c),
                "author_login": self._login(c.get("user"), "login"),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
                "body": c.get("body") or "",
                "url": c.get("html_url"),
            }
            for c in comments
        ]

    def fetch_pr_reviews(self, owner: str, name: str, pr_number: int) -> list[dict[str, Any]]:
        reviews = self._paginate(f"/repos/{owner}/{name}/pulls/{pr_number}/reviews")
        return [
            {
                "id": str(r["id"]),
                "repo": f"{owner}/{name}",
                "pr_number": pr_number,
                "author_login": self._login(r.get("user"), "login"),
                "state": r.get("state"),
                "submitted_at": r.get("submitted_at"),
                "url": r.get("html_url"),
            }
            for r in reviews
        ]

    def fetch_pr_review_comments(
        self, owner: str, name: str, pr_number: int, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        cutoff = since.astimezone(timezone.utc) if since else None
        reviews = self._paginate(f"/repos/{owner}/{name}/pulls/{pr_number}/reviews")
        records: list[dict[str, Any]] = []
        for review in reviews:
            review_id = review.get("id")
            try:
                comments = self._paginate(
                    f"/repos/{owner}/{name}/pulls/{pr_number}/reviews/{review_id}/comments"
                )
            except requests.HTTPError as exc:
                logger.warning("Could not list review comments for %s#%s: %s", f"{owner}/{name}", pr_number, exc)
                continue
            for c in comments:
                created = _parse_ts(c.get("created_at"))
                if cutoff is not None and created is not None and created < cutoff:
                    continue
                records.append({
                    "id": str(c["id"]),
                    "repo": f"{owner}/{name}",
                    "pr_number": pr_number,
                    "review_id": review_id,
                    "author_login": self._login(c.get("user"), "login"),
                    "created_at": c.get("created_at"),
                    "updated_at": c.get("updated_at"),
                    "url": c.get("html_url"),
                })
        return records

    def fetch_releases(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        releases = self._paginate(f"/repos/{owner}/{name}/releases")
        cutoff = since.astimezone(timezone.utc)
        records: list[dict[str, Any]] = []
        for r in releases:
            published = r.get("published_at")
            published_ts = _parse_ts(published)
            if published_ts is None or published_ts < cutoff:
                continue
            records.append({
                "id": str(r["id"]),
                "repo": f"{owner}/{name}",
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "published_at": published,
                "author_login": self._login(r.get("author"), "login"),
                "prerelease": r.get("prerelease", False),
                "url": r.get("html_url"),
            })
        return records

    def fetch_latest_release(self, owner: str, name: str) -> dict[str, Any] | None:
        """Return the latest stable release, or None when the repo has none."""
        try:
            releases = self._paginate(f"/repos/{owner}/{name}/releases")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise
        for r in releases:
            if r.get("draft") or r.get("prerelease"):
                continue
            return {
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "published_at": r.get("published_at"),
                "url": r.get("html_url"),
                "prerelease": False,
            }
        return None

    def fetch_repo_snapshot(self, owner: str, name: str) -> dict[str, Any]:
        data = self._get(f"/repos/{owner}/{name}")
        latest_release = self.fetch_latest_release(owner, name)
        snapshot_at = datetime.now(timezone.utc)
        open_items = (data.get("open_issues_count", 0) or 0) + (data.get("open_pr_counter", 0) or 0)
        return {
            "id": f"{owner}/{name}@{snapshot_at.strftime('%Y-%m-%d')}",
            "repo": f"{owner}/{name}",
            "snapshot_at": snapshot_at.isoformat(),
            "stars": data.get("stars_count", 0) or 0,
            "forks": data.get("forks_count", 0) or 0,
            "open_issues": open_items,
            "watchers": data.get("watchers_count", 0) or 0,
            "pushed_at": data.get("updated_at"),
            "default_branch": data.get("default_branch"),
            "latest_release_tag": (latest_release or {}).get("tag_name"),
            "latest_release_name": (latest_release or {}).get("name"),
            "latest_release_published_at": (latest_release or {}).get("published_at"),
            "latest_release_url": (latest_release or {}).get("url"),
        }

    @staticmethod
    def _state_param(states: list[str]) -> str:
        wanted = {s.upper() for s in states}
        if wanted == {"OPEN"}:
            return "open"
        if wanted == {"CLOSED"}:
            return "closed"
        return "all"


def _dig(data: Any, *keys: str) -> str | None:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) and current else None


def _actor_login(user: dict[str, Any] | None, email: str | None) -> str | None:
    """Prefer the linked Forgejo login, then the email handle, then the name."""
    login = _dig(user, "login")
    if login:
        return login
    if email and "@" in email:
        return email.split("@", 1)[0]
    return None


def _any_before(batch: list[dict[str, Any]], cutoff: datetime, keys: tuple[str, ...]) -> bool:
    """True when any row's timestamp at `keys` is older than `cutoff`."""
    for row in batch:
        timestamp = _parse_ts(_dig(row, *keys))
        if timestamp is not None and timestamp < cutoff:
            return True
    return False


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _issue_number(comment: dict[str, Any]) -> str:
    for key in ("issue_url", "pull_request_url"):
        url = comment.get(key)
        if isinstance(url, str) and url:
            tail = url.rstrip("/").split("/")[-1]
            if tail.isdigit():
                return tail
    return ""
