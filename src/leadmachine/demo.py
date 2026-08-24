"""Genereert per lead een complete voorbeeldsite.

Dit is je belangrijkste verkoopmiddel: geen 'ik kan een site voor u maken',
maar 'hier staat hij al, met uw naam, adres en openingstijden erin'.
De pagina is bewust gemarkeerd als voorbeeld, zodat niemand hem kan aanzien
voor de officiele website van het bedrijf.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import OUT_DIR, REPO_ROOT
from .demo_data import eigen_omschrijving, feiten_uit_tags, parse_opening_hours, sociale_links

TEMPLATE_DIR = REPO_ROOT / "templates"

# Per branche een eigen kleur en toon. Niet willekeurig: een hovenier hoort
# groen te zijn en een garage niet roze. Alle accentkleuren halen minstens
# 4,5:1 op wit, zodat ze ook als tekst leesbaar zijn.
THEMAS: dict[str, dict[str, str]] = {
    "kapper": {"accent": "#b45309", "accent_donker": "#f59e0b", "tint": "#fef6ec",
               "sfeer": "Vakmanschap en rust"},
    "schoonheidssalon": {"accent": "#be185d", "accent_donker": "#f472b6", "tint": "#fdf2f7",
                         "sfeer": "Verzorging met aandacht"},
    "aannemer": {"accent": "#c2410c", "accent_donker": "#fb923c", "tint": "#fff5ed",
                 "sfeer": "Afspraak is afspraak"},
    "installateur": {"accent": "#0369a1", "accent_donker": "#38bdf8", "tint": "#eff8ff",
                     "sfeer": "Snel ter plaatse"},
    "garage": {"accent": "#b91c1c", "accent_donker": "#f87171", "tint": "#fef4f4",
               "sfeer": "Eerlijk advies"},
    "hovenier": {"accent": "#15803d", "accent_donker": "#4ade80", "tint": "#f1fdf4",
                 "sfeer": "Groen dat blijft staan"},
    "restaurant": {"accent": "#9a3412", "accent_donker": "#fb923c", "tint": "#fff6ee",
                   "sfeer": "Lekker eten, zonder gedoe"},
}
STANDAARD_THEMA = {"accent": "#1d4ed8", "accent_donker": "#60a5fa", "tint": "#eef4ff",
                   "sfeer": "Gewoon goed geregeld"}

# Iconen als pad in een 24x24 vak. Zelf getekend, dus geen extern bestand nodig
# en de pagina blijft in een keer laden.
ICONEN: dict[str, str] = {
    "schaar": "M6 4a2 2 0 1 0 0 4 2 2 0 0 0 0-4Zm0 12a2 2 0 1 0 0 4 2 2 0 0 0 0-4ZM8 8l12 10M8 16 20 6",
    "kleur": "M12 3c-3 4-5 6.5-5 9a5 5 0 0 0 10 0c0-2.5-2-5-5-9Z",
    "spiegel": "M12 3a5 7 0 1 0 0 14 5 7 0 0 0 0-14ZM12 17v4M9 21h6",
    "hand": "M8 13V5a1.5 1.5 0 0 1 3 0v6m0-1V4a1.5 1.5 0 0 1 3 0v7m0-2a1.5 1.5 0 0 1 3 0v6a6 6 0 0 1-6 6h-1a6 6 0 0 1-6-6v-3a1.5 1.5 0 0 1 3 0",
    "bloem": "M12 8a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm0-5v5m0 6v5m-6.5-9.5 4 2.5m5 3 4 2.5m0-8-4 2.5m-5 3-4 2.5",
    "hamer": "m14 6 4 4M3 21l7-7M13 3l8 8-3 3-8-8 3-3ZM10 8l-7 7 3 3",
    "huis": "M3 11.5 12 4l9 7.5M5.5 10v9.5h13V10M10 20v-5h4v5",
    "schild": "M12 3 5 6v6c0 4 3 7.5 7 9 4-1.5 7-5 7-9V6l-7-3Z",
    "moersleutel": "M14.5 4a5 5 0 0 0-4.6 7L4 16.9 7.1 20l5.9-5.9A5 5 0 1 0 14.5 4Z",
    "druppel": "M12 3.5C9 7.5 7 10 7 12.5a5 5 0 0 0 10 0C17 10 15 7.5 12 3.5Z",
    "bliksem": "M13 3 5 14h6l-1 7 8-11h-6l1-7Z",
    "auto": "M5 16h14M6.5 16V12l1.6-4.2A2 2 0 0 1 10 6.5h4a2 2 0 0 1 1.9 1.3L17.5 12v4M7 19v-3m10 3v-3M6 12h12",
    "wiel": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
    "meter": "M12 20a8 8 0 1 1 8-8M12 12l4-3",
    "blad": "M20 4C10 4 4 9 4 16c0 2 .7 3.3.7 3.3S9 12 20 10c0 0-4 8-12 9",
    "schep": "M12 3v10m0 0-3-3m3 3 3-3M7 15h10l-1.5 6h-7L7 15Z",
    "bord": "M7 3v8a3 3 0 0 0 6 0V3M10 3v18M17 3c-1.5 2-2 4-2 6s.5 3 2 3v9",
    "kok": "M7 21h10v-6H7v6ZM7 15a4 4 0 0 1-1-7.9 4 4 0 0 1 7.5-1.8A4 4 0 0 1 18 7.4 4 4 0 0 1 17 15",
    "agenda": "M4 6.5h16v14H4v-14ZM8 3v5M16 3v5M4 11h16",
    "telefoon": "M6 3.5h3l1.5 4-2 1.5a12 12 0 0 0 5.5 5.5l1.5-2 4 1.5v3a2 2 0 0 1-2.2 2A16.5 16.5 0 0 1 4 6.7 2 2 0 0 1 6 3.5Z",
    "pin": "M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11Zm0-8.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
    "klok": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 3.5V12l3 2",
    "kaart": "M3 7.5h18v10H3v-10ZM3 11h18",
    "munt": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 4v8m-2-6h3a1.8 1.8 0 0 1 0 3.6h-2a1.8 1.8 0 0 0 0 3.6h3",
    "wifi": "M5 11a10 10 0 0 1 14 0M8 14.5a5.5 5.5 0 0 1 8 0M12 18.5h.01",
    "zon": "M12 7.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9ZM12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M5 5l1.8 1.8M17.2 17.2 19 19M19 5l-1.8 1.8M6.8 17.2 5 19",
    "wind": "M4 9h10a2.5 2.5 0 1 0-2.5-2.5M4 14h13a2.5 2.5 0 1 1-2.5 2.5",
    "tas": "M6 8h12l-1 12H7L6 8Zm3 0V6a3 3 0 0 1 6 0v2",
    "bezorgen": "M3 7h11v9H3V7Zm11 3h4l3 3v3h-7v-6ZM7 19a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Zm10 0a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z",
    "toegankelijk": "M12 4.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3ZM10 9.5h4v4h3.5M8 12a5 5 0 1 0 7 5.5",
    "persoon": "M12 4a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7ZM5 21a7 7 0 0 1 14 0",
    "personen": "M9 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM3 20a6 6 0 0 1 12 0M16 11a3 3 0 1 0 0-6M17 20h4a5 5 0 0 0-3-4.6",
    "verboden": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16ZM6.5 6.5l11 11",
    "vlag": "M6 21V4m0 0h11l-2 3.5L17 11H6",
    "kringloop": "M7 7h7l-2-2m2 12H7l2 2M4.5 14 7 7m10 3 2.5 7",
    "sleutel": "M14.5 4a5 5 0 0 0-4.6 7L4 16.9 7.1 20l5.9-5.9A5 5 0 1 0 14.5 4Z",
    "kwast": "M9 14h6v3a3 3 0 0 1-6 0v-3ZM7 4h10v10H7V4Z",
    "vink": "m5 12.5 4.5 4.5L19 7",
}

# Drie diensten per branche, met een icoon dat erbij hoort.
DIENSTEN: dict[str, list[dict[str, str]]] = {
    "kapper": [
        {"titel": "Knippen", "icoon": "schaar",
         "tekst": "Voor dames, heren en kinderen. Met afspraak, en als het kan ook zonder."},
        {"titel": "Kleuren en highlights", "icoon": "kleur",
         "tekst": "Eerst even overleggen wat bij je past, daarna pas de kwast erin."},
        {"titel": "Verzorging en advies", "icoon": "spiegel",
         "tekst": "Producten en tips waarmee het thuis ook goed blijft zitten."},
    ],
    "schoonheidssalon": [
        {"titel": "Gezichtsbehandeling", "icoon": "bloem",
         "tekst": "Afgestemd op jouw huid, in alle rust en zonder haast."},
        {"titel": "Ontharen en wenkbrauwen", "icoon": "schaar",
         "tekst": "Zorgvuldig werk waar je weken plezier van hebt."},
        {"titel": "Massage", "icoon": "hand",
         "tekst": "Een uur waarin je even helemaal niets hoeft."},
    ],
    "aannemer": [
        {"titel": "Verbouwing", "icoon": "hamer",
         "tekst": "Van eerste schets tot oplevering, met een vast aanspreekpunt."},
        {"titel": "Aanbouw en dakkapel", "icoon": "huis",
         "tekst": "Inclusief tekening en vergunningsaanvraag, zodat u dat niet hoeft uit te zoeken."},
        {"titel": "Onderhoud en reparatie", "icoon": "moersleutel",
         "tekst": "Kozijnen, daken, lekkages. Ook voor de kleinere klussen."},
    ],
    "installateur": [
        {"titel": "Storing en spoed", "icoon": "bliksem",
         "tekst": "Geen water of geen stroom? Bel gerust, we komen zo snel als het kan."},
        {"titel": "Installatie en aanleg", "icoon": "druppel",
         "tekst": "Cv, leidingwerk of groepenkast, netjes uitgevoerd volgens de norm."},
        {"titel": "Onderhoud", "icoon": "agenda",
         "tekst": "Een jaarlijkse beurt voorkomt dat u er in januari zonder zit."},
    ],
    "garage": [
        {"titel": "APK", "icoon": "vink",
         "tekst": "Klaar terwijl u wacht, met eerlijk advies over wat echt moet en wat kan wachten."},
        {"titel": "Onderhoud en reparatie", "icoon": "moersleutel",
         "tekst": "Alle merken, altijd een prijsopgave voordat we beginnen."},
        {"titel": "Banden en seizoenswissel", "icoon": "wiel",
         "tekst": "Inclusief opslag van uw andere set, zodat u ze thuis niet kwijt kunt."},
    ],
    "hovenier": [
        {"titel": "Ontwerp", "icoon": "bloem",
         "tekst": "Een plan dat past bij uw huis, uw budget en de tijd die u erin wilt steken."},
        {"titel": "Aanleg", "icoon": "schep",
         "tekst": "Bestrating, beplanting en verlichting, in een keer goed gedaan."},
        {"titel": "Onderhoud", "icoon": "blad",
         "tekst": "Periodiek langskomen, zodat het het hele jaar door verzorgd blijft."},
    ],
    "restaurant": [
        {"titel": "De kaart", "icoon": "bord",
         "tekst": "Wisselende gerechten, waar het kan met producten uit de streek."},
        {"titel": "Reserveren", "icoon": "agenda",
         "tekst": "Een tafel vastleggen kan straks rechtstreeks via deze pagina."},
        {"titel": "Groepen en feesten", "icoon": "kok",
         "tekst": "Ook voor verjaardagen, borrels en zakelijke etentjes."},
    ],
}

STANDAARD_DIENSTEN = [
    {"titel": "Wat we doen", "icoon": "vink",
     "tekst": "Een duidelijk overzicht van waar klanten bij ons voor terechtkunnen."},
    {"titel": "Werkgebied", "icoon": "pin",
     "tekst": "Waar we actief zijn, zodat mensen uit de buurt ons weten te vinden."},
    {"titel": "Afspraak maken", "icoon": "telefoon",
     "tekst": "Bellen, mailen of straks rechtstreeks via deze pagina een aanvraag doen."},
]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value) or "bedrijf"


# Verandert het ontwerp, dan hoort dit nummer op te lopen. De cyclus bouwt
# demo's met een ouder nummer opnieuw, zodat een verbetering ook terechtkomt
# bij bedrijven waarvoor al eerder een pagina is gemaakt.
DEMO_VERSIE = "2"


def thema_van(niche: str) -> dict[str, str]:
    return THEMAS.get(niche, STANDAARD_THEMA)


KOPPEN = {
    "kapper": "Goed geknipt, zonder lang wachten",
    "schoonheidssalon": "Even tijd voor uzelf, vlakbij huis",
    "aannemer": "Vakwerk dat af is wanneer we het beloven",
    "installateur": "Snel geholpen bij storing en onderhoud",
    "garage": "Uw auto in vertrouwde handen",
    "hovenier": "Een tuin waar u het hele jaar van geniet",
    "restaurant": "Lekker eten, zonder gedoe",
}


def _kop(niche: str, niche_label: str, stad: str | None) -> str:
    if niche in KOPPEN:
        return KOPPEN[niche]
    waar = f" in {stad}" if stad else " bij u in de buurt"
    return f"{niche_label.capitalize()}{waar} waar u zo terechtkunt"


def _intro(naam: str, niche_label: str, stad: str | None, tags: dict[str, Any]) -> str:
    """Heeft het bedrijf zelf een omschrijving achtergelaten, dan is die beter
    dan wat wij kunnen bedenken."""
    eigen = eigen_omschrijving(tags)
    if eigen:
        return eigen
    waar = f"{stad} en omgeving" if stad else "de regio"
    return (
        f"{naam} is een {niche_label} in {waar}. Hier ziet u in een oogopslag wat we "
        "doen, wanneer we open zijn en hoe u ons bereikt, zodat u daarvoor niet eerst "
        "hoeft te bellen."
    )


def demo_slug(lead: Any) -> str:
    get = lead.get if isinstance(lead, dict) else (lambda k, d=None: lead[k] if k in lead.keys() else d)
    return slugify(f"{get('name')}-{get('osm_id') or get('id') or ''}")


def build_demo(lead: Any, campaign: Any) -> tuple[str, str]:
    """Bouwt de pagina en geeft (slug, html) terug, zonder iets weg te schrijven.
    Live draait alles zonder schijf, dus het schrijven is een aparte stap."""
    return demo_slug(lead), _render(lead, campaign)


def render_demo(lead: Any, campaign: Any, out_dir: Path | None = None) -> Path:
    """Bouwt de pagina en zet hem als bestand neer (lokaal gebruik)."""
    slug, html = build_demo(lead, campaign)
    base = Path(out_dir) if out_dir else OUT_DIR / "demos"
    target = base / slug
    target.mkdir(parents=True, exist_ok=True)
    page = target / "index.html"
    page.write_text(html, encoding="utf-8")
    return page


def _render(lead: Any, campaign: Any) -> str:
    get = lead.get if isinstance(lead, dict) else (lambda k, d=None: lead[k] if k in lead.keys() else d)
    naam = get("name")
    niche = get("niche") or ""
    niche_obj = campaign.niche(niche) if hasattr(campaign, "niche") else None
    niche_label = niche_obj.label if niche_obj else (niche or "bedrijf")
    stad = get("city")

    tags = get("raw") or {}
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            tags = {}

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # Let op: de bestandsnaam eindigt op .j2, dus die extensie moet mee -
        # anders escapet Jinja niets en is een OSM-naam met HTML erin een lek.
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("demo_site.html.j2")

    straat, nummer = get("street"), get("housenumber")
    adres = " ".join(deel for deel in [straat, nummer] if deel) or None
    lat, lon = get("lat"), get("lon")
    telefoon = get("phone") or ""
    week = parse_opening_hours(get("opening_hours"))

    return template.render(
        lead={
            "naam": naam, "stad": stad, "postcode": get("postcode"),
            "telefoon": telefoon, "email": get("email"),
        },
        niche_label=niche_label,
        thema=thema_van(niche),
        kop=_kop(niche, niche_label, stad),
        intro=_intro(naam, niche_label, stad, tags),
        meta_omschrijving=(
            f"{naam} - {niche_label}{' in ' + stad if stad else ''}. "
            "Openingstijden, diensten en contactgegevens op een rij."
        ),
        diensten=DIENSTEN.get(niche, STANDAARD_DIENSTEN),
        iconen=ICONEN,
        week=week,
        openingstijden_tekst=get("opening_hours") or "",
        feiten=feiten_uit_tags(tags),
        socials=sociale_links(tags),
        adres=adres,
        route_url=(
            f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
            if lat and lon else None
        ),
        telefoon_href=re.sub(r"[^\d+]", "", telefoon),
        afzender=campaign.outreach,
        jaar=date.today().year,
    )
