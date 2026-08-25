"""Track changes in the Cashu Mint Directory JSON feed."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import MINT_DIRECTORY_URL, MINT_SNAPSHOT_DIR


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_mint_directory() -> list[dict[str, Any]]:
    resp = requests.get(MINT_DIRECTORY_URL, timeout=60)
    resp.raise_for_status()
    return resp.json()


def latest_snapshot_path() -> Path | None:
    paths = sorted(MINT_SNAPSHOT_DIR.glob("*_mints.json"), reverse=True)
    return paths[0] if paths else None


def normalize_mint(mint: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, comparable view of a mint record."""
    return {
        "url": mint.get("url"),
        "status": mint.get("status"),
        "name": mint.get("name"),
        "implementation": mint.get("implementation"),
        "version": mint.get("version"),
        "nuts": sorted(mint.get("nuts", [])),
        "units": sorted(mint.get("units", [])),
        "email": mint.get("email"),
        "x": mint.get("x"),
        "nostr": mint.get("nostr"),
        "other_contact": mint.get("other_contact"),
        "icon_url": mint.get("icon_url"),
        "description": mint.get("description"),
        "description_long": mint.get("description_long"),
        "mint_methods": mint.get("mint_methods", []),
        "melt_methods": mint.get("melt_methods", []),
        "nut4_disabled": mint.get("nut4_disabled"),
        "nut5_disabled": mint.get("nut5_disabled"),
    }


def key(mint: dict[str, Any]) -> str:
    return mint.get("url") or mint.get("name") or str(mint)


def summarize_changes(
    current: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> list[str]:
    cur_by_key = {key(m): m for m in current}
    prev_by_key = {key(m): m for m in previous}

    added = [k for k in cur_by_key if k not in prev_by_key]
    removed = [k for k in prev_by_key if k not in cur_by_key]
    changed: list[tuple[str, list[str]]] = []

    for k, cur in cur_by_key.items():
        prev = prev_by_key.get(k)
        if not prev:
            continue
        cur_norm = normalize_mint(cur)
        prev_norm = normalize_mint(prev)
        diffs: list[str] = []
        for field in sorted(cur_norm.keys()):
            if cur_norm[field] != prev_norm[field]:
                if field in ("nuts", "units"):
                    old_set = set(prev_norm[field])
                    new_set = set(cur_norm[field])
                    added_items = sorted(new_set - old_set)
                    removed_items = sorted(old_set - new_set)
                    if added_items:
                        diffs.append(f"+{field}: {added_items}")
                    if removed_items:
                        diffs.append(f"-{field}: {removed_items}")
                elif field in ("mint_methods", "melt_methods"):
                    diffs.append(f"{field} changed")
                else:
                    diffs.append(f"{field}: {prev_norm[field]} → {cur_norm[field]}")
        if diffs:
            changed.append((cur.get("name") or k, diffs))

    bullets: list[str] = []
    for k in added:
        m = cur_by_key[k]
        bullets.append(f"New mint: {m.get('name', k)} ({m.get('url', 'no URL')})")
    for k in removed:
        m = prev_by_key[k]
        bullets.append(f"Removed mint: {m.get('name', k)} ({m.get('url', 'no URL')})")
    for name, diffs in changed:
        bullets.append(f"{name}: " + "; ".join(diffs))
    return bullets


def save_snapshot(mints: list[dict[str, Any]], date_str: str | None = None) -> Path:
    MINT_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = date_str or _now()
    path = MINT_SNAPSHOT_DIR / f"{date_str}_mints.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(mints, f, indent=2, ensure_ascii=False)
    return path


def load_snapshot(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
