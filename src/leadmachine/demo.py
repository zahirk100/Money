"""Genereert per lead een complete voorbeeldsite.

Dit is je belangrijkste verkoopmiddel: geen 'ik kan een site voor u maken',
maar 'hier staat hij al, met uw naam, adres en openingstijden erin'.
De pagina is bewust gemarkeerd als voorbeeld, zodat niemand hem kan aanzien
voor de officiele website van het bedrijf.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import OUT_DIR, REPO_ROOT

TEMPLATE_DIR = REPO_ROOT / "templates"

DAY_MAP = {
    "Mo": "Maandag", "Tu": "Dinsdag", "We": "Woensdag", "Th": "Donderdag",
    "Fr": "Vrijdag", "Sa": "Zaterdag", "Su": "Zondag",
    "Ma": "Maandag", "Di": "Dinsdag", "Wo": "Woensdag", "Do": "Donderdag",
    "Vr": "Vrijdag", "Za": "Zaterdag", "Zo": "Zondag",
}

PALETTE = ["#1f6feb", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#0369a1", "#4d7c0f"]

SERVICES: dict[str, list[dict[str, str]]] = {
    "kapper": [
        {"title": "Knippen", "text": "Voor dames, heren en kinderen - met of zonder afspraak."},
        {"title": "Kleuren & highlights", "text": "Advies over wat bij je past, zonder verrassingen achteraf."},
        {"title": "Verzorging", "text": "Behandelingen en producten om het thuis goed te houden."},
    ],
    "schoonheidssalon": [
        {"title": "Gezichtsbehandeling", "text": "Een behandeling afgestemd op je huid, in alle rust."},
        {"title": "Ontharen & wenkbrauwen", "text": "Snel, zorgvuldig en met blijvend resultaat."},
        {"title": "Massage", "text": "Even helemaal niets hoeven, midden in de week."},
    ],
    "aannemer": [
        {"title": "Verbouwing", "text": "Van doorbraak tot oplevering, met een vaste contactpersoon."},
        {"title": "Aanbouw & dakkapel", "text": "Inclusief tekeningen en de vergunningsaanvraag."},
        {"title": "Onderhoud", "text": "Kozijnen, daken en reparaties - ook kleine klussen."},
    ],
    "installateur": [
        {"title": "Storing & reparatie", "text": "Snel ter plaatse als het water of de stroom het laat afweten."},
        {"title": "Nieuwe installatie", "text": "Cv, leidingwerk of groepenkast, netjes volgens de norm."},
        {"title": "Onderhoud", "text": "Jaarlijkse beurt zodat je niet in de winter zonder zit."},
    ],
    "garage": [
        {"title": "APK", "text": "Klaar terwijl je wacht, inclusief eerlijk advies over wat echt moet."},
        {"title": "Onderhoud & reparatie", "text": "Alle merken, met een prijsopgave vooraf."},
        {"title": "Banden & seizoenswissel", "text": "Inclusief opslag van je andere set."},
    ],
    "hovenier": [
        {"title": "Tuinontwerp", "text": "Een plan dat past bij je huis, je budget en je tijd."},
        {"title": "Aanleg", "text": "Bestrating, beplanting en verlichting in een keer goed."},
        {"title": "Onderhoud", "text": "Periodiek onderhoud zodat het het hele jaar netjes blijft."},
    ],
    "restaurant": [
        {"title": "De kaart", "text": "Wisselende gerechten met producten uit de buurt."},
        {"title": "Reserveren", "text": "Een tafel vastleggen kan straks direct via de site."},
        {"title": "Groepen & feesten", "text": "Ook voor verjaardagen, borrels en vergaderingen."},
    ],
}

DEFAULT_SERVICES = [
    {"title": "Onze diensten", "text": "Een duidelijk overzicht van wat je klanten bij je kunnen krijgen."},
    {"title": "Werkgebied", "text": "Waar je actief bent, zodat mensen in de buurt je vinden."},
    {"title": "Afspraak maken", "text": "Bellen, mailen of straks direct online een aanvraag doen."},
]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value) or "bedrijf"


def brand_color(name: str) -> str:
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def parse_hours(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    rows: list[dict[str, str]] = []
    for block in raw.split(";"):
        block = block.strip()
        if not block:
            continue
        parts = block.split(None, 1)
        if len(parts) != 2:
            rows.append({"days": block, "time": ""})
            continue
        days, times = parts
        rows.append({"days": _translate_days(days), "time": times.replace(",", ", ")})
    return rows


def _translate_days(days: str) -> str:
    def swap(match: re.Match[str]) -> str:
        return DAY_MAP.get(match.group(0), match.group(0))

    return re.sub(r"[A-Za-z]{2}", swap, days)


HEADLINES = {
    "kapper": "Goed geknipt, zonder lang wachten",
    "schoonheidssalon": "Even tijd voor uzelf, vlakbij huis",
    "aannemer": "Vakwerk dat af is wanneer we het beloven",
    "installateur": "Snel geholpen bij storing, onderhoud en nieuwbouw",
    "garage": "Uw auto in vertrouwde handen",
    "hovenier": "Een tuin waar u het hele jaar van geniet",
    "restaurant": "Lekker eten, zonder gedoe",
}


def _headline(niche: str, niche_label: str, city: str | None) -> str:
    if niche in HEADLINES:
        return HEADLINES[niche]
    where = f" in {city}" if city else " bij u in de buurt"
    return f"{niche_label.capitalize()}{where} waar u zo terecht kunt"


def _intro(name: str, niche_label: str, city: str | None) -> str:
    where = f"{city} en omgeving" if city else "de regio"
    return (
        f"{name} is een {niche_label} in {where}. Op deze pagina staat in een oogopslag "
        "wat we doen, wanneer we open zijn en hoe u ons bereikt - zodat u niet eerst "
        "hoeft te bellen om dat uit te zoeken."
    )


def render_demo(lead: Any, campaign: Any, out_dir: Path | None = None) -> Path:
    get = lead.get if isinstance(lead, dict) else (lambda k, d=None: lead[k] if k in lead.keys() else d)
    name = get("name")
    niche = get("niche") or ""
    niche_obj = campaign.niche(niche) if hasattr(campaign, "niche") else None
    niche_label = niche_obj.label if niche_obj else (niche or "bedrijf")
    city = get("city")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # Let op: de bestandsnaam eindigt op .j2, dus die extensie moet mee -
        # anders escapet Jinja niets en is een OSM-naam met HTML erin een lek.
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("demo_site.html.j2")

    street, number = get("street"), get("housenumber")
    address = " ".join(p for p in [street, number] if p) or None
    lat, lon = get("lat"), get("lon")
    maps_url = (
        f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
        if lat and lon else None
    )
    phone = get("phone") or ""

    html = template.render(
        lead={
            "name": name, "city": city, "postcode": get("postcode"),
            "phone": phone, "email": get("email"),
        },
        niche_label=niche_label,
        headline=_headline(niche, niche_label, city),
        intro=_intro(name, niche_label, city),
        meta_description=(
            f"{name} - {niche_label}{' in ' + city if city else ''}. "
            "Openingstijden, diensten en contactgegevens op een rij."
        ),
        services=SERVICES.get(niche, DEFAULT_SERVICES),
        hours=parse_hours(get("opening_hours")),
        address=address,
        maps_url=maps_url,
        phone_href=re.sub(r"[^\d+]", "", phone),
        brand=brand_color(name or "bedrijf"),
        sender=campaign.outreach,
        year=date.today().year,
    )

    base = Path(out_dir) if out_dir else OUT_DIR / "demos"
    target = base / slugify(f"{name}-{get('osm_id') or ''}")
    target.mkdir(parents=True, exist_ok=True)
    page = target / "index.html"
    page.write_text(html, encoding="utf-8")
    return page
