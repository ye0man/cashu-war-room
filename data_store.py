"""Incremental JSONL-backed data store for raw GitHub extracts."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import RAW_DIR


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class DataStore:
    """Loads and appends records as JSONL under data/raw/<owner>/<repo>/<entity>.jsonl."""

    def __init__(self, base_dir: Path = RAW_DIR) -> None:
        self.base_dir = base_dir

    def _path(self, repo_full_name: str, entity: str) -> Path:
        owner, name = repo_full_name.split("/", 1)
        path = self.base_dir / owner / name
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{entity}.jsonl"

    def load_records(self, repo_full_name: str, entity: str) -> list[dict[str, Any]]:
        path = self._path(repo_full_name, entity)
        if not path.exists():
            return []
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def load_all(self, entity: str) -> dict[str, list[dict[str, Any]]]:
        """Load all records for an entity keyed by repo full name."""
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for repo_dir in self.base_dir.rglob(entity + ".jsonl"):
            owner = repo_dir.parent.parent.name
            name = repo_dir.parent.name
            repo_full = f"{owner}/{name}"
            result[repo_full].extend(self.load_records(repo_full, entity))
        return dict(result)

    def append_records(
        self,
        repo_full_name: str,
        entity: str,
        records: list[dict[str, Any]],
        id_field: str = "id",
    ) -> tuple[int, int]:
        """Append new records, replacing existing ones with the same id.

        Returns (added, updated) counts.
        """
        path = self._path(repo_full_name, entity)
        existing = self.load_records(repo_full_name, entity)
        by_id: dict[str, dict[str, Any]] = {r.get(id_field): r for r in existing if r.get(id_field)}
        added = 0
        updated = 0
        for record in records:
            rid = record.get(id_field)
            if rid is None:
                # No id; append as-is and hope for the best.
                existing.append(record)
                added += 1
                continue
            if rid in by_id:
                by_id[rid] = record
                updated += 1
            else:
                by_id[rid] = record
                added += 1
        merged = list(by_id.values())
        with path.open("w", encoding="utf-8") as f:
            for record in merged:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return added, updated

    def get_max_datetime(
        self, repo_full_name: str, entity: str, date_field: str
    ) -> datetime | None:
        records = self.load_records(repo_full_name, entity)
        max_dt: datetime | None = None
        for record in records:
            ts = _parse_ts(record.get(date_field))
            if ts and (max_dt is None or ts > max_dt):
                max_dt = ts
        return max_dt

    def get_max_datetime_global(self, entity: str, date_field: str) -> datetime | None:
        all_records = self.load_all(entity)
        max_dt: datetime | None = None
        for records in all_records.values():
            for record in records:
                ts = _parse_ts(record.get(date_field))
                if ts and (max_dt is None or ts > max_dt):
                    max_dt = ts
        return max_dt
