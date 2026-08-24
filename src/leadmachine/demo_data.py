"""Alles wat we uit de gegevens van een bedrijf kunnen halen voor de demopagina.

Hoe meer van het bedrijf zelf op die pagina staat, hoe eerder een ondernemer
denkt: dit gaat over mij. OpenStreetMap bevat vaak veel meer dan naam en adres -
keuken, betaalmogelijkheden, terras, welke reparaties een garage doet - en dat
is precies het soort detail dat een sjabloon tot een voorstel maakt.

Niets hierin verzint iets. Staat het er niet, dan tonen we het niet: verzonnen
recensies of cijfers op een pagina met iemands echte bedrijfsnaam zijn niet
alleen misleidend, ze vallen ook meteen door de mand.
"""

from __future__ import annotations

import re
from typing import Any

DAGEN = [
    ("ma", "Maandag"), ("di", "Dinsdag"), ("wo", "Woensdag"), ("do", "Donderdag"),
    ("vr", "Vrijdag"), ("za", "Zaterdag"), ("zo", "Zondag"),
]
DAG_NUMMER = {
    "mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6,
    "ma": 0, "di": 1, "wo": 2, "do": 3, "vr": 4, "za": 5, "zo": 6,
}


def parse_opening_hours(raw: str | None) -> list[dict[str, Any]]:
    """Zet de OSM-notatie om in zeven dagen met tijden.

    Bijvoorbeeld "Mo-Fr 08:00-17:00; Sa 09:00-13:00" wordt een week waarin
    zaterdag korter open is en zondag dicht. Lukt het niet, dan geven we een
    lege lijst terug en toont de pagina de tekst zoals hij is.
    """
    if not raw or not raw.strip():
        return []

    tekst = raw.strip()
    week: list[dict[str, Any]] = [
        {"kort": kort, "naam": naam, "tijden": [], "gesloten": True} for kort, naam in DAGEN
    ]

    if tekst.lower().replace(" ", "") in {"24/7", "24uur", "altijdopen"}:
        for dag in week:
            dag.update(tijden=["00:00-24:00"], gesloten=False)
        return week

    herkend = False
    for blok in tekst.split(";"):
        blok = blok.strip()
        if not blok or blok.lower().startswith(("ph", "sh")):
            continue  # feestdagen en schoolvakanties laten we buiten beschouwing

        deel = blok.split(None, 1)
        if len(deel) == 1:
            # Alleen tijden, zonder dagen: geldt dan voor de hele week.
            dagen, tijden = "Mo-Su", deel[0]
        else:
            dagen, tijden = deel[0], deel[1]

        nummers = _dagnummers(dagen)
        if not nummers:
            continue

        gesloten = tijden.strip().lower() in {"off", "closed", "gesloten"}
        blokken = [] if gesloten else _tijdblokken(tijden)
        if not gesloten and not blokken:
            continue

        for nummer in nummers:
            week[nummer].update(tijden=blokken, gesloten=gesloten or not blokken)
        herkend = True

    return week if herkend else []


def _dagnummers(spec: str) -> list[int]:
    nummers: list[int] = []
    for stuk in spec.split(","):
        stuk = stuk.strip().lower()
        if "-" in stuk:
            van, _, tot = stuk.partition("-")
            start, eind = DAG_NUMMER.get(van[:2]), DAG_NUMMER.get(tot[:2])
            if start is None or eind is None:
                continue
            nummer = start
            while True:
                nummers.append(nummer)
                if nummer == eind:
                    break
                nummer = (nummer + 1) % 7
        elif stuk[:2] in DAG_NUMMER:
            nummers.append(DAG_NUMMER[stuk[:2]])
    return sorted(set(nummers))


def _tijdblokken(spec: str) -> list[str]:
    return [
        f"{gevonden[0]}-{gevonden[1]}"
        for gevonden in re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", spec)
    ]


# Tags die iets zeggen wat een klant wil weten. Alleen tonen als ze er staan.
FEITEN: dict[str, tuple[str, str]] = {
    # sleutel=waarde                    label                      pictogram
    "wheelchair=yes": ("Rolstoeltoegankelijk", "toegankelijk"),
    "wheelchair=limited": ("Beperkt toegankelijk", "toegankelijk"),
    "outdoor_seating=yes": ("Terras", "zon"),
    "takeaway=yes": ("Afhalen mogelijk", "tas"),
    "delivery=yes": ("Wij bezorgen", "bezorgen"),
    "drive_through=yes": ("Drive-through", "auto"),
    "internet_access=wlan": ("Gratis wifi", "wifi"),
    "air_conditioning=yes": ("Airconditioning", "wind"),
    "payment:cards=yes": ("Pinnen mogelijk", "kaart"),
    "payment:debit_cards=yes": ("Pinnen mogelijk", "kaart"),
    "payment:credit_cards=yes": ("Creditcard welkom", "kaart"),
    "payment:cash=yes": ("Contant kan ook", "munt"),
    "payment:contactless=yes": ("Contactloos betalen", "kaart"),
    "diet:vegetarian=yes": ("Vegetarische opties", "blad"),
    "diet:vegan=yes": ("Veganistische opties", "blad"),
    "diet:gluten_free=yes": ("Glutenvrij mogelijk", "blad"),
    "organic=yes": ("Biologisch", "blad"),
    "smoking=no": ("Rookvrij", "verboden"),
    "reservation=required": ("Reserveren nodig", "agenda"),
    "reservation=recommended": ("Reserveren aanbevolen", "agenda"),
    "male=yes": ("Ook voor heren", "persoon"),
    "female=yes": ("Ook voor dames", "persoon"),
    "unisex=yes": ("Dames en heren", "personen"),
    "parking=yes": ("Eigen parkeerplaats", "auto"),
    "second_hand=yes": ("Ook tweedehands", "kringloop"),
    "service:vehicle:tyres=yes": ("Banden", "wiel"),
    "service:vehicle:car_repair=yes": ("Reparatie", "sleutel"),
    "service:vehicle:electrical=yes": ("Elektronica", "bliksem"),
    "service:vehicle:air_conditioning=yes": ("Airco service", "wind"),
    "service:vehicle:diagnostics=yes": ("Diagnose", "meter"),
    "service:vehicle:oil_change=yes": ("Olie verversen", "druppel"),
    "service:vehicle:painting=yes": ("Spuitwerk", "kwast"),
    "service:vehicle:body_repair=yes": ("Schadeherstel", "schild"),
    "service:vehicle:new_car_sales=yes": ("Verkoop nieuw", "auto"),
    "service:vehicle:used_car_sales=yes": ("Verkoop occasions", "auto"),
}

KEUKENS = {
    "dutch": "Hollandse keuken", "italian": "Italiaans", "chinese": "Chinees",
    "turkish": "Turks", "indian": "Indiaas", "japanese": "Japans", "sushi": "Sushi",
    "greek": "Grieks", "french": "Frans", "spanish": "Spaans", "thai": "Thais",
    "mexican": "Mexicaans", "pizza": "Pizza", "burger": "Burgers", "kebab": "Kebab",
    "seafood": "Vis en zeevruchten", "steak_house": "Steakhouse", "vegetarian": "Vegetarisch",
    "asian": "Aziatisch", "indonesian": "Indonesisch", "surinamese": "Surinaams",
    "coffee_shop": "Koffie", "sandwich": "Broodjes", "ice_cream": "IJs", "cafe": "Cafe",
}


def feiten_uit_tags(tags: dict[str, Any]) -> list[dict[str, str]]:
    """De opsomming met wat dit bedrijf te bieden heeft."""
    gevonden: list[dict[str, str]] = []
    gezien: set[str] = set()

    keuken = (tags.get("cuisine") or "").split(";")[0].strip().lower()
    if keuken:
        gevonden.append({
            "tekst": KEUKENS.get(keuken, keuken.replace("_", " ").capitalize()),
            "icoon": "bord",
        })

    for sleutel, (label, icoon) in FEITEN.items():
        naam, _, waarde = sleutel.partition("=")
        if str(tags.get(naam, "")).strip().lower() == waarde and label not in gezien:
            gevonden.append({"tekst": label, "icoon": icoon})
            gezien.add(label)

    sinds = (tags.get("start_date") or tags.get("established") or "").strip()
    jaar = re.match(r"(\d{4})", sinds)
    if jaar:
        gevonden.insert(0, {"tekst": f"Sinds {jaar.group(1)}", "icoon": "vlag"})

    return gevonden[:8]


def eigen_omschrijving(tags: dict[str, Any]) -> str | None:
    """Sommige bedrijven hebben zelf een omschrijving in OSM gezet. Die is altijd
    beter dan wat wij eromheen kunnen bedenken."""
    for sleutel in ("description:nl", "description", "note"):
        tekst = (tags.get(sleutel) or "").strip()
        if 20 <= len(tekst) <= 400:
            return tekst
    return None


def sociale_links(tags: dict[str, Any]) -> list[dict[str, str]]:
    kanalen = [
        ("facebook", ("contact:facebook", "facebook")),
        ("instagram", ("contact:instagram", "instagram")),
        ("linkedin", ("contact:linkedin", "linkedin")),
        ("whatsapp", ("contact:whatsapp", "whatsapp")),
    ]
    uit: list[dict[str, str]] = []
    for naam, sleutels in kanalen:
        for sleutel in sleutels:
            waarde = (tags.get(sleutel) or "").strip()
            if waarde:
                if not waarde.startswith("http"):
                    waarde = f"https://{naam}.com/{waarde.lstrip('@/')}"
                uit.append({"naam": naam, "url": waarde})
                break
    return uit
