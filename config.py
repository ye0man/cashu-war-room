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
    forge: str = "github"
    host: str = "github.com"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.full_name}"

    @property
    def is_github(self) -> bool:
        return self.forge == "github"

    @property
    def is_forgejo(self) -> bool:
        return self.forge == "forgejo"


REPOS = [
    Repo("cashubtc", "cdk", "cdk"),
    Repo("cashubtc", "coco", "coco"),
    Repo("cashubtc", "cashu-ts", "cashu-ts"),
    Repo("cashubtc", "cashu.me", "cashu.me"),
    Repo("cashubtc", "nuts", "nuts"),
    Repo("cashubtc", "nutshell", "nutshell"),
    Repo("cashubtc", "Numo", "numo"),
    Repo("cashubtc", "wallet", "wallet"),
    Repo("SovranBitcoin", "Sovran", "sovran"),
    Repo("zeugmaster", "CashuSwift", "cashu-swift"),
    Repo("zeugmaster", "macadamia", "macadamia"),
    Repo("zeugmaster", "pecan", "pecan"),
    Repo("cashubtc", "orchard", "orchard"),
    Repo("cashubtc", "cdk-payment-processors", "cdk-payment-processors"),
    Repo("cashubtc", "docs.cashu.space", "docs"),
    Repo("cashubtc", "cashu-skill", "cashu-skill"),
    # Forgejo-hosted repositories.
    Repo("cashubtc", "internet-mint-finder", "internet-mint-finder", forge="forgejo", host="git.cashu.dev"),
]

REPO_NAMES = [r.full_name for r in REPOS]

# Web base per tracked repo, so links point at the right forge.
REPO_WEB_BASE = {r.full_name: f"https://{r.host}" for r in REPOS}


def repo_web_url(full_name: str) -> str:
    base = REPO_WEB_BASE.get(full_name, "https://github.com")
    return f"{base}/{full_name}"


def person_web_url(login: str, repo: str | None = None) -> str:
    base = REPO_WEB_BASE.get(repo, "https://github.com") if repo else "https://github.com"
    return f"{base}/{login}"


MINT_DIRECTORY_URL = "https://ye0man.github.io/cashu-mint-directory/mints.json"

LOOKBACK_DAYS = 7
REPORT_RETENTION_DAYS = 30

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


def forgejo_token() -> str | None:
    return os.environ.get("FORGEJO_TOKEN") or os.environ.get("GITEA_TOKEN")
