"""Shared configuration for the Cashu War Room report."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    display: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


REPOS = [
    Repo("cashubtc", "cdk", "cdk"),
    Repo("cashubtc", "coco", "coco"),
    Repo("cashubtc", "cashu-ts", "cashu-ts"),
    Repo("cashubtc", "nuts", "nuts"),
    Repo("cashubtc", "nutshell", "nutshell"),
    Repo("cashubtc", "Numo", "numo"),
    Repo("cashubtc", "wallet", "wallet"),
]

REPO_NAMES = [r.full_name for r in REPOS]

MINT_DIRECTORY_URL = "https://ye0man.github.io/cashu-mint-directory/mints.json"

LOOKBACK_DAYS = 365

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
MINT_SNAPSHOT_DIR = DATA_DIR / "mint_snapshots"
REPORT_DIR = ROOT_DIR / "reports"
TEMPLATE_DIR = ROOT_DIR / "templates"


def today() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def default_start_date(end: datetime | None = None) -> datetime:
    end = end or today()
    return end - timedelta(days=LOOKBACK_DAYS)


def github_token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
