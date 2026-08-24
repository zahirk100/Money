"""Leads ophalen uit OpenStreetMap via de Overpass API.

Waarom OSM en niet Google Maps: OSM-data is open (ODbL) en de API is bedoeld
om bevraagd te worden. Google Maps scrapen is in strijd met hun voorwaarden.
Bijkomend voordeel: juist de bedrijven die zelf niets aan hun online
aanwezigheid doen, staan in OSM zonder website-tag. Dat is precies je doelgroep.
"""

from __future__ import annotations

import json
import random
import re
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


# Tags waarmee een bedrijf een eigen website aangeeft.
WEBSITE_TAGS = ("website", "contact:website", "url")


def build_query(
    campaign: Campaign,
    niche: Niche,
    timeout: int = 25,
    area: str | None = None,
    alleen_zonder_website: bool = True,
) -> str:
    """Bouwt de Overpass-opdracht.

    Standaard vraagt hij alleen bedrijven zonder website op. Dat scheelt niet
    alleen een lijst vol bedrijven die je toch niet gaat benaderen: zulke leads
    hoeven ook niet gecontroleerd te worden, en dat is verreweg de traagste stap.
    """
    zonder = "".join(f'[!"{tag}"]' for tag in WEBSITE_TAGS) if alleen_zonder_website else ""
    selectors = []
    for raw in niche.filters:
        key, _, value = raw.partition("=")
        key, value = key.strip(), value.strip()
        selectors.append(
            f'  nwr["{key}"="{value}"]{zonder}(area.searchArea);' if value
            else f'  nwr["{key}"]{zonder}(area.searchArea);'
        )
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'area["name"="{area or campaign.zoekgebied()}"]["boundary"="administrative"]'
        f'["admin_level"="{campaign.admin_level}"]->.searchArea;\n'
        "(\n" + "\n".join(selectors) + "\n);\n"
        "out center tags;"
    )


def _per_sleutel(niches: Iterable[Niche]) -> dict[str, list[str]]:
    """Alle gezochte tags gegroepeerd per sleutel: {"shop": ["bakery", ...]}."""
    groepen: dict[str, list[str]] = {}
    for niche in niches:
        for raw in niche.filters:
            key, _, value = raw.partition("=")
            key, value = key.strip(), value.strip()
            waarden = groepen.setdefault(key, [])
            if value and value not in waarden:
                waarden.append(value)
    return groepen


def build_query_gebied(
    campaign: Campaign,
    area: str,
    timeout: int = 25,
    alleen_zonder_website: bool = True,
    niches: list[Niche] | None = None,
) -> str:
    """Een gemeente in een keer, in plaats van een opdracht per branche.

    Dit is het verschil tussen veertig zoekopdrachten per gemeente en een. Een
    losse opdracht als "hovenier in Dongen" komt vaak met nul terug - niet omdat
    er geen hoveniers zijn, maar omdat er in OpenStreetMap maar een paar honderd
    van dat soort bedrijven in heel Nederland staan. Zo'n opdracht kost dan wel
    de tijd van een hele beurt. Vragen we alle branches tegelijk, dan levert
    dezelfde seconde alles op wat er in die gemeente te halen valt, en zoeken we
    achteraf zelf uit bij welke branche elk bedrijf hoort.
    """
    lijst = list(niches if niches is not None else campaign.niches)
    zonder = "".join(f'[!"{tag}"]' for tag in WEBSITE_TAGS) if alleen_zonder_website else ""
    selectors = []
    for key, waarden in _per_sleutel(lijst).items():
        if waarden:
            keuze = "|".join(sorted(set(waarden)))
            selectors.append(f'  nwr["{key}"~"^({keuze})$"]{zonder}(area.searchArea);')
        else:
            selectors.append(f'  nwr["{key}"]{zonder}(area.searchArea);')
    return (
        f"[out:json][timeout:{timeout}];\n"
        f'area["name"="{area}"]["boundary"="administrative"]'
        f'["admin_level"="{campaign.admin_level}"]->.searchArea;\n'
        "(\n" + "\n".join(selectors) + "\n);\n"
        "out center tags;"
    )


def niche_van(tags: dict[str, Any], niches: Iterable[Niche]) -> str | None:
    """Bij welke branche hoort dit bedrijf? De eerste die past wint, dus de
    volgorde in de config bepaalt wie voorgaat bij een bedrijf dat op twee
    lijstjes staat (een autobedrijf dat ook auto's verkoopt bijvoorbeeld)."""
    for niche in niches:
        for raw in niche.filters:
            key, _, value = raw.partition("=")
            key, value = key.strip(), value.strip()
            aanwezig = str(tags.get(key, "")).strip()
            if aanwezig and (aanwezig == value or not value):
                return niche.name
    return None


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


# Wat Overpass terugstuurt als hij niet wil, in gewone taal. Deze tekst komt
# in het dashboard terecht, dus "429" alleen is niet genoeg.
OVERPASS_REDENEN = {
    429: "te veel verzoeken achter elkaar (429)",
    504: "de server had te lang nodig (504)",
    503: "server tijdelijk niet beschikbaar (503)",
    502: "server gaf een foutmelding door (502)",
}


def _reden(status: int) -> str:
    return OVERPASS_REDENEN.get(status, f"foutcode {status}")


def fetch_overpass(query: str, timeout: float = 30.0) -> dict[str, Any]:
    """Wachten heeft een grens. Een serverless functie leeft maar kort, dus een
    trage query moet opgeven voordat het platform de hele functie afkapt.

    Lukt het bij geen van de servers, dan staat in de foutmelding per server
    waarom. Zonder die reden staat er in het dashboard alleen dat het niet
    lukte, en dat is precies de melding waar je niets aan hebt.
    """
    redenen: list[str] = []
    # Niet altijd bij dezelfde server beginnen: dan raakt die als eerste vol.
    volgorde = list(OVERPASS_ENDPOINTS)
    random.shuffle(volgorde)
    for endpoint in volgorde:
        naam = endpoint.split("/")[2]
        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                timeout=timeout,
                headers={"User-Agent": "LeadMachine/1.0 (OSM lead research)"},
            )
            if resp.status_code >= 400:
                uitleg = _reden(resp.status_code)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    # Bij een afgekeurde opdracht staat in het antwoord waarom.
                    # Dat is precies wat je wilt zien; "foutcode 400" niet.
                    kern = " ".join(re.sub(r"<[^>]+>", " ", resp.text).split())[:200]
                    if kern:
                        uitleg += f' - "{kern}"'
                redenen.append(f"{naam}: {uitleg}")
                # Druk bij Overpass. Even wachten heeft alleen zin als daar tijd
                # voor is; anders meteen de andere server proberen.
                if resp.status_code == 429 and timeout > 20:
                    time.sleep(5)
                continue
            return resp.json()
        except requests.Timeout:
            redenen.append(f"{naam}: gaf binnen {timeout:.0f} seconden geen antwoord")
        except requests.RequestException as exc:
            redenen.append(f"{naam}: geen verbinding ({type(exc).__name__})")
        except ValueError:
            redenen.append(f"{naam}: stuurde geen bruikbaar antwoord terug")
    raise RuntimeError("Overpass gaf niets terug - " + "; ".join(redenen))


def discover(
    campaign: Campaign,
    source: str = "overpass",
    fixture_path: str | Path | None = None,
    only_niche: str | None = None,
    pause: float = 3.0,
    timeout: float = 30.0,
    area: str | None = None,
    alleen_zonder_website: bool = True,
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

    gebied = area or campaign.zoekgebied()

    if only_niche is None:
        # Alle branches in een opdracht. Zie build_query_gebied waarom.
        payload = fetch_overpass(
            build_query_gebied(
                campaign, gebied, timeout=max(10, int(timeout) - 5),
                alleen_zonder_website=alleen_zonder_website, niches=niches,
            ),
            timeout=timeout,
        )
        for element in payload.get("elements", []):
            naam = niche_van(element.get("tags") or {}, niches)
            if not naam:
                continue
            lead = element_to_lead(element, naam, "overpass")
            if not lead:
                continue
            if alleen_zonder_website and lead.get("website"):
                continue
            yield lead
        return

    for index, niche in enumerate(niches):
        if index:
            time.sleep(pause)  # Overpass is gratis; niet leegtrekken
        payload = fetch_overpass(
            build_query(
                campaign, niche, timeout=max(10, int(timeout) - 5), area=gebied,
                alleen_zonder_website=alleen_zonder_website,
            ),
            timeout=timeout,
        )
        for element in payload.get("elements", []):
            lead = element_to_lead(element, niche.name, "overpass")
            if not lead:
                continue
            # Vangnet: een enkele keer staat er toch een adres in dat wij als
            # website lezen (bijvoorbeeld via een tag die we niet uitsloten).
            if alleen_zonder_website and lead.get("website"):
                continue
            yield lead
