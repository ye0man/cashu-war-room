"""GitHub API client combining GraphQL for bulk data and REST for edge cases."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import github_token

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_REST_URL = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token or github_token()
        if not self.token:
            logger.warning("No GH_TOKEN provided; GraphQL will fail and REST rate limits are low.")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = self.session.post(GITHUB_GRAPHQL_URL, json=payload)
        if resp.status_code == 401:
            raise PermissionError("GitHub GraphQL request unauthorized. Check GH_TOKEN.")
        if resp.status_code == 403:
            # Rate limit or abuse
            reset_at = int(resp.headers.get("x-ratelimit-reset", time.time() + 60))
            wait = max(1, reset_at - int(time.time()) + 1)
            logger.warning("GitHub rate limit hit; sleeping %ds", wait)
            time.sleep(wait)
            resp = self.session.post(GITHUB_GRAPHQL_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            logger.error("GraphQL errors: %s", data["errors"])
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return data.get("data", {})

    def _rest_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{GITHUB_REST_URL}{path}"
        while True:
            resp = self.session.get(url, params=params)
            if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
                reset_at = int(resp.headers.get("x-ratelimit-reset", time.time() + 60))
                wait = max(1, reset_at - int(time.time()) + 1)
                logger.warning("REST rate limit hit; sleeping %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()

    def _rest_paginate(
        self, path: str, params: dict[str, Any] | None = None, per_page: int = 100
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        params["per_page"] = per_page
        params["page"] = 1
        results: list[dict[str, Any]] = []
        while True:
            page = self._rest_get(path, params)
            if not isinstance(page, list):
                break
            results.extend(page)
            if len(page) < per_page:
                break
            params["page"] += 1
        return results

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # GraphQL paginated fetchers
    # ------------------------------------------------------------------

    def fetch_commits(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        query = """
        query($owner: String!, $name: String!, $since: GitTimestamp!, $after: String) {
          repository(owner: $owner, name: $name) {
            defaultBranchRef {
              target {
                ... on Commit {
                  history(since: $since, after: $after, first: 100) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      oid
                      message
                      authoredDate
                      committedDate
                      author { user { login } }
                      committer { user { login } }
                      additions
                      deletions
                      changedFiles
                    }
                  }
                }
              }
            }
          }
        }
        """
        records: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = self._graphql(query, {"owner": owner, "name": name, "since": self._iso(since), "after": after})
            history = data.get("repository", {}).get("defaultBranchRef", {}).get("target", {}).get("history", {})
            for node in history.get("nodes", []):
                records.append({
                    "id": node["oid"],
                    "repo": f"{owner}/{name}",
                    "sha": node["oid"],
                    "message": node["message"],
                    "authored_at": node["authoredDate"],
                    "committed_at": node["committedDate"],
                    "author_login": (node.get("author") or {}).get("user", {}).get("login"),
                    "committer_login": (node.get("committer") or {}).get("user", {}).get("login"),
                    "additions": node.get("additions", 0),
                    "deletions": node.get("deletions", 0),
                    "changed_files": node.get("changedFiles", 0),
                    "url": f"https://github.com/{owner}/{name}/commit/{node['oid']}",
                })
            page_info = history.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return records

    def fetch_issues(
        self, owner: str, name: str, since: datetime, states: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch issues updated since `since`. Captures state changes on older records."""
        query = """
        query($owner: String!, $name: String!, $after: String) {
          repository(owner: $owner, name: $name) {
            issues(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC},
                   states: [OPEN, CLOSED]) {
              pageInfo { hasNextPage endCursor }
              nodes {
                id
                number
                title
                state
                createdAt
                closedAt
                updatedAt
                author { login }
                assignees(first: 10) { nodes { login } }
                labels(first: 20) { nodes { name } }
                comments(first: 1) { totalCount }
                url
              }
            }
          }
        }
        """
        records: list[dict[str, Any]] = []
        after: str | None = None
        cutoff = since.astimezone(timezone.utc)
        while True:
            data = self._graphql(query, {"owner": owner, "name": name, "after": after})
            issues = data.get("repository", {}).get("issues", {})
            for node in issues.get("nodes", []):
                updated = datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00"))
                if updated < cutoff:
                    return records
                records.append({
                    "id": node["id"],
                    "repo": f"{owner}/{name}",
                    "number": node["number"],
                    "title": node["title"],
                    "state": node["state"],
                    "created_at": node["createdAt"],
                    "closed_at": node.get("closedAt"),
                    "updated_at": node["updatedAt"],
                    "author_login": node.get("author", {}).get("login"),
                    "assignee_logins": [a["login"] for a in node.get("assignees", {}).get("nodes", [])],
                    "labels": [l["name"] for l in node.get("labels", {}).get("nodes", [])],
                    "comments_count": node.get("comments", {}).get("totalCount", 0),
                    "url": node["url"],
                })
            page_info = issues.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return records

    def fetch_pull_requests(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        """Fetch PRs updated since `since`."""
        query = """
        query($owner: String!, $name: String!, $after: String) {
          repository(owner: $owner, name: $name) {
            pullRequests(first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC},
                         states: [OPEN, CLOSED, MERGED]) {
              pageInfo { hasNextPage endCursor }
              nodes {
                id
                number
                title
                state
                createdAt
                mergedAt
                closedAt
                updatedAt
                author { login }
                mergedBy { login }
                assignees(first: 10) { nodes { login } }
                labels(first: 20) { nodes { name } }
                additions
                deletions
                changedFiles
                comments(first: 1) { totalCount }
                reviews(first: 1) { totalCount }
                reviewThreads(first: 1) { totalCount }
                url
              }
            }
          }
        }
        """
        records: list[dict[str, Any]] = []
        after: str | None = None
        cutoff = since.astimezone(timezone.utc)
        while True:
            data = self._graphql(query, {"owner": owner, "name": name, "after": after})
            prs = data.get("repository", {}).get("pullRequests", {})
            for node in prs.get("nodes", []):
                updated = datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00"))
                if updated < cutoff:
                    return records
                records.append({
                    "id": node["id"],
                    "repo": f"{owner}/{name}",
                    "number": node["number"],
                    "title": node["title"],
                    "state": node["state"],
                    "created_at": node["createdAt"],
                    "merged_at": node.get("mergedAt"),
                    "closed_at": node.get("closedAt"),
                    "updated_at": node["updatedAt"],
                    "author_login": node.get("author", {}).get("login"),
                    "merged_by_login": node.get("mergedBy", {}).get("login"),
                    "assignee_logins": [a["login"] for a in node.get("assignees", {}).get("nodes", [])],
                    "labels": [l["name"] for l in node.get("labels", {}).get("nodes", [])],
                    "additions": node.get("additions", 0),
                    "deletions": node.get("deletions", 0),
                    "changed_files": node.get("changedFiles", 0),
                    "comments_count": node.get("comments", {}).get("totalCount", 0),
                    "reviews_count": node.get("reviews", {}).get("totalCount", 0),
                    "review_threads_count": node.get("reviewThreads", {}).get("totalCount", 0),
                    "url": node["url"],
                })
            page_info = prs.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
        return records

    # ------------------------------------------------------------------
    # REST fetchers for data GraphQL makes awkward
    # ------------------------------------------------------------------

    def fetch_issue_comments(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{name}/issues/comments"
        params = {"since": self._iso(since), "per_page": 100}
        comments = self._rest_paginate(path, params)
        return [
            {
                "id": str(c["id"]),
                "repo": f"{owner}/{name}",
                "issue_number": c.get("issue_url", "").split("/")[-1],
                "author_login": c.get("user", {}).get("login"),
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "body": c.get("body", ""),
                "url": c["html_url"],
            }
            for c in comments
        ]

    def fetch_pr_reviews(self, owner: str, name: str, pr_number: int) -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{name}/pulls/{pr_number}/reviews"
        reviews = self._rest_paginate(path)
        return [
            {
                "id": str(r["id"]),
                "repo": f"{owner}/{name}",
                "pr_number": pr_number,
                "author_login": r.get("user", {}).get("login"),
                "state": r.get("state"),
                "submitted_at": r.get("submitted_at"),
                "url": r.get("html_url"),
            }
            for r in reviews
        ]

    def fetch_pr_review_comments(
        self, owner: str, name: str, pr_number: int, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{name}/pulls/{pr_number}/comments"
        params: dict[str, Any] = {"per_page": 100}
        if since:
            params["since"] = self._iso(since)
        comments = self._rest_paginate(path, params)
        return [
            {
                "id": str(c["id"]),
                "repo": f"{owner}/{name}",
                "pr_number": pr_number,
                "review_id": c.get("pull_request_review_id"),
                "author_login": c.get("user", {}).get("login"),
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "url": c["html_url"],
            }
            for c in comments
        ]

    def fetch_releases(self, owner: str, name: str, since: datetime) -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{name}/releases"
        releases = self._rest_paginate(path)
        records = []
        for r in releases:
            published = r.get("published_at")
            if not published:
                continue
            published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if published_dt < since.astimezone(timezone.utc):
                continue
            records.append({
                "id": str(r["id"]),
                "repo": f"{owner}/{name}",
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "published_at": published,
                "author_login": r.get("author", {}).get("login"),
                "prerelease": r.get("prerelease", False),
                "url": r.get("html_url"),
            })
        return records

    def fetch_repo_snapshot(self, owner: str, name: str) -> dict[str, Any]:
        path = f"/repos/{owner}/{name}"
        data = self._rest_get(path)
        return {
            "id": f"{owner}/{name}",
            "repo": f"{owner}/{name}",
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "watchers": data.get("watchers_count", 0),
            "pushed_at": data.get("pushed_at"),
            "default_branch": data.get("default_branch"),
        }

    def search_nut_items(
        self, owner: str, name: str, state: str = "open", per_page: int = 100
    ) -> list[dict[str, Any]]:
        """Search issues/PRs in a repo whose title or body references NUT-XX."""
        query = f"repo:{owner}/{name} is:{state} NUT- in:title,body"
        path = "/search/issues"
        params = {"q": query, "per_page": per_page, "page": 1}
        results: list[dict[str, Any]] = []
        while True:
            data = self._rest_get(path, params)
            items = data.get("items", [])
            for item in items:
                results.append({
                    "id": str(item["id"]),
                    "repo": f"{owner}/{name}",
                    "number": item["number"],
                    "title": item["title"],
                    "state": item["state"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                    "closed_at": item.get("closed_at"),
                    "author_login": item.get("user", {}).get("login"),
                    "labels": [l["name"] for l in item.get("labels", [])],
                    "is_pr": "pull_request" in item,
                    "url": item["html_url"],
                })
            if len(items) < per_page:
                break
            params["page"] += 1
        return results
