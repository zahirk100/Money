"""Zoekt of een bedrijf toch een website heeft die OpenStreetMap niet kent.

Waarom dit moet: in OpenStreetMap ontbreekt de website-tag bij verreweg de
meeste bedrijven, ook bij bedrijven die een prima site hebben. "Geen website in
OSM" betekent dus niet "geen website". Toch was dat precies wat de mail beweerde,
en niets verbrandt een lead sneller dan een ondernemer die zijn eigen site opent
en leest dat hij er geen heeft.

Dit is geen zoekmachine en doet ook niet alsof. We proberen een handvol voor de
hand liggende domeinnamen en kijken of daar echt dit bedrijf staat. Vinden we
niets, dan weten we alleen dat wij niets konden vinden - en zo staat het ook in
de mail.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Woorden die in honderden bedrijfsnamen voorkomen. Een pagina die "kapsalon"
# zegt bewijst niets; een pagina die "dijkzicht" zegt wel.
ALGEMEEN = {
    "aannemersbedrijf", "administratiekantoor", "autobedrijf", "bakkerij", "bloemenzaak",
    "bouwbedrijf", "cafe", "cafetaria", "eetcafe", "garage", "hotel", "installatiebedrijf",
    "kapper", "kapsalon", "kliniek", "praktijk", "restaurant", "salon", "schoonheidssalon",
    "slagerij", "snackbar", "sportschool", "tandartspraktijk", "winkel", "zaak",
    "van", "de", "den", "der", "het", "en", "aan", "bij", "voor", "vof", "bv",
    "b.v.", "v.o.f.", "the", "and",
}

# Meer dan dit proberen we niet: elke poging kost tijd, en hoe verder je van de
# voor de hand liggende naam af gaat, hoe kleiner de kans dat het klopt.
MAX_POGINGEN = 3


def _plat(tekst: str) -> str:
    tekst = unicodedata.normalize("NFKD", tekst or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", tekst.lower()).strip()


def kernwoorden(naam: str) -> list[str]:
    """De woorden waaraan je dit bedrijf herkent, zonder de algemene termen."""
    return [w for w in _plat(naam).split() if len(w) >= 4 and w not in ALGEMEEN]


def kandidaten(naam: str) -> list[str]:
    """Domeinnamen die dit bedrijf redelijkerwijs zou kunnen hebben."""
    # Losse letters zijn resten van "B.V." en "V.O.F." en horen niet in een
    # domeinnaam thuis.
    woorden = [w for w in _plat(naam).split() if len(w) > 1]
    zonder_ruis = [w for w in woorden if w not in ALGEMEEN] or woorden
    if not zonder_ruis:
        return []
    opties = [
        "".join(woorden),          # kapsalondijkzicht.nl
        "".join(zonder_ruis),      # dijkzicht.nl
        "-".join(zonder_ruis),     # dijk-zicht.nl
    ]
    uniek: list[str] = []
    for optie in opties:
        optie = optie.strip("-")
        # Een domeinnaam van drie letters raad je niet; dat levert alleen maar
        # toevallige treffers op bij een heel ander bedrijf.
        if len(optie) >= 6 and optie not in uniek:
            uniek.append(optie)
    return [f"https://www.{deel}.nl" for deel in uniek[:MAX_POGINGEN]]


def _herkent_bedrijf(html: str, naam: str, lead: Any) -> bool:
    """Staat dit bedrijf ook echt op die pagina?

    Zonder deze controle plakken we een willekeurige site op een lead, en dat is
    erger dan niets vinden: dan beoordelen we de site van iemand anders.
    """
    tekst = _plat(html)
    for woord in kernwoorden(naam):
        if woord in tekst:
            return True
    # Een telefoonnummer is net zo goed bewijs, en vaak zelfs beter.
    get = lead.get if isinstance(lead, dict) else (lambda k, d=None: lead[k] if k in lead.keys() else d)
    telefoon = re.sub(r"\D", "", str(get("phone") or ""))
    if len(telefoon) >= 9:
        cijfers = re.sub(r"\D", "", html)
        if telefoon in cijfers or telefoon.lstrip("0") in cijfers:
            return True
    return False


def zoek_website(lead: Any, client: Any, timeout: float = 6.0) -> str | None:
    """Het adres van de website van dit bedrijf, of None als we niets vonden.

    None betekent uitdrukkelijk 'niet gevonden', niet 'bestaat niet'.
    """
    get = lead.get if isinstance(lead, dict) else (lambda k, d=None: lead[k] if k in lead.keys() else d)
    naam = str(get("name") or "")
    if not naam:
        return None

    for url in kandidaten(naam):
        try:
            gehaald = client.get(url, pogingen=1)
        except Exception:  # noqa: BLE001 - een mislukte gok is geen fout
            continue
        if not getattr(gehaald, "ok", False):
            continue
        if _herkent_bedrijf(gehaald.html or "", naam, lead):
            return getattr(gehaald, "final_url", None) or url
    return None
