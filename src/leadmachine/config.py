"""Laden en valideren van de campagne-configuratie."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "campaign.yaml"
DEFAULT_DB = REPO_ROOT / "data" / "leads.db"
OUT_DIR = REPO_ROOT / "out"


class ConfigError(Exception):
    """De configuratie mist iets waar we niet omheen kunnen."""


@dataclass
class Niche:
    name: str
    label: str
    filters: list[str]


@dataclass
class Campaign:
    area: str
    admin_level: int
    niches: list[Niche]
    audit: dict[str, Any]
    scoring: dict[str, Any]
    outreach: dict[str, Any]
    offer: dict[str, Any]
    path: Path | None = None
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def hot_threshold(self) -> int:
        return int(self.scoring.get("hot_threshold", 60))

    @property
    def warm_threshold(self) -> int:
        return int(self.scoring.get("warm_threshold", 35))

    def segment(self, score: int) -> str:
        if score >= self.hot_threshold:
            return "hot"
        if score >= self.warm_threshold:
            return "warm"
        return "cold"

    def niche(self, name: str) -> Niche | None:
        return next((n for n in self.niches if n.name == name), None)

    def require_sender(self) -> dict[str, str]:
        """Afzendergegevens zijn wettelijk verplicht in commerciele mail."""
        needed = ["sender_name", "sender_email", "company_name", "company_address"]
        missing = [k for k in needed if not str(self.outreach.get(k, "")).strip()]
        if missing:
            raise ConfigError(
                "Vul eerst deze velden onder 'outreach' in de campagne-config in: "
                + ", ".join(missing)
                + ".\nZonder afzendergegevens mag je geen commerciele mail sturen."
            )
        return {k: str(self.outreach[k]) for k in self.outreach if isinstance(self.outreach[k], (str, int))}


def load_campaign(path: str | Path | None = None) -> Campaign:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        example = cfg_path.with_name("campaign.example.yaml")
        hint = f"\nKopieer {example} naar {cfg_path} en pas hem aan." if example.exists() else ""
        raise ConfigError(f"Config niet gevonden: {cfg_path}{hint}")

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    region = raw.get("region") or {}
    if not region.get("area"):
        raise ConfigError("region.area ontbreekt in de config (bijv. 'Zwolle').")

    niches = [
        Niche(
            name=n["name"],
            label=n.get("label", n["name"]),
            filters=list(n.get("filters") or []),
        )
        for n in raw.get("niches") or []
        if n.get("name")
    ]
    if not niches:
        raise ConfigError("Geen niches gedefinieerd in de config.")

    return Campaign(
        area=str(region["area"]),
        admin_level=int(region.get("admin_level", 8)),
        niches=niches,
        audit=raw.get("audit") or {},
        scoring=raw.get("scoring") or {},
        outreach=raw.get("outreach") or {},
        offer=raw.get("offer") or {},
        path=cfg_path,
        _raw=raw,
    )


def load_dotenv(path: str | Path | None = None) -> None:
    """Minimale .env-loader zodat er geen extra dependency nodig is."""
    env_path = Path(path) if path else REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
