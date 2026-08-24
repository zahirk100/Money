"""Leads ophalen uit OpenStreetMap via de Overpass API.

Waarom OSM en niet Google Maps: OSM-data is open (ODbL) en de API is bedoeld
om bevraagd te worden. Google Maps scrapen is in strijd met hun voorwaarden.
Bijkomend voordeel: juist de bedrijven die zelf niets aan hun online
aanwezigheid doen, staan in OSM zonder website-tag. Dat is precies je doelgroep.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from .config import Campaign, Niche

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

FIXTURE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_osm.json"


def build_query(campaign: Campaign, niche: Niche, timeout: int = 25) -> str:
    selectors = []
    for raw in niche.filters:
        key, _, value = raw.partition("=")
        key, value = key.strip(), value.strip()
        selectors.append(
            f'  nwr["{key}"="{value}"](area.searchArea);' if value
            else f'  nwr["{key}"](area.searchArea);'
        )
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'area["name"="{campaign.area}"]["boundary"="administrative"]'
        f'["admin_level"="{campaign.admin_level}"]->.searchArea;\n'
        "(\n" + "\n".join(selectors) + "\n);\n"
        "out center tags;"
    )


def _first(tags: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return None


def element_to_lead(element: dict[str, Any], niche_name: str, source: str) -> dict[str, Any] | None:
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None  # naamloze objecten zijn onbruikbaar voor outreach

    website = _first(tags, "website", "contact:website", "url")
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website.lstrip("/")

    email = _first(tags, "email", "contact:email")
    if email and email.lower().startswith("mailto:"):
        email = email[7:]

    center = element.get("center") or {}
    return {
        "osm_type": element.get("type", "node"),
        "osm_id": str(element.get("id")),
        "name": name,
        "niche": niche_name,
        "street": tags.get("addr:street"),
        "housenumber": tags.get("addr:housenumber"),
        "postcode": tags.get("addr:postcode"),
        "city": tags.get("addr:city"),
        "phone": _first(tags, "phone", "contact:phone", "contact:mobile"),
        "email": email,
        "website": website,
        "opening_hours": tags.get("opening_hours"),
        "lat": element.get("lat", center.get("lat")),
        "lon": element.get("lon", center.get("lon")),
        "source": source,
        "raw": tags,
    }


def fetch_overpass(query: str, timeout: float = 30.0) -> dict[str, Any]:
    """Wachten heeft een grens. Een serverless functie leeft maar kort, dus een
    trage query moet opgeven voordat het platform de hele functie afkapt."""
    last_error: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                timeout=timeout,
                headers={"User-Agent": "LeadMachine/1.0 (OSM lead research)"},
            )
            if resp.status_code == 429:
                # Druk bij Overpass. Even wachten heeft alleen zin als daar tijd
                # voor is; anders meteen de andere server proberen.
                if timeout > 20:
                    time.sleep(5)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Overpass onbereikbaar ({last_error}).")


def discover(
    campaign: Campaign,
    source: str = "overpass",
    fixture_path: str | Path | None = None,
    only_niche: str | None = None,
    pause: float = 3.0,
    timeout: float = 30.0,
) -> Iterable[dict[str, Any]]:
    """Levert lead-dicts op. source='fixture' draait volledig offline."""
    niches = [n for n in campaign.niches if not only_niche or n.name == only_niche]

    if source == "fixture":
        data = json.loads(Path(fixture_path or FIXTURE).read_text(encoding="utf-8"))
        wanted = {n.name for n in niches}
        for element in data.get("elements", []):
            niche_name = (element.get("tags") or {}).get("x:niche", "onbekend")
            if niche_name not in wanted:
                continue
            lead = element_to_lead(element, niche_name, "fixture")
            if lead:
                yield lead
        return

    for index, niche in enumerate(niches):
        if index:
            time.sleep(pause)  # Overpass is gratis; niet leegtrekken
        payload = fetch_overpass(
            build_query(campaign, niche, timeout=max(10, int(timeout) - 5)), timeout=timeout
        )
        for element in payload.get("elements", []):
            lead = element_to_lead(element, niche.name, "overpass")
            if lead:
                yield lead
