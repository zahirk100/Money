"""Nederlandse gemeenten om automatisch uit te putten.

In de automatische stand trekt de machine zelf combinaties van gemeente en
branche uit deze lijst, zodat je niet zelf hoeft te bedenken waar je gaat
zoeken. De namen staan zoals ze in OpenStreetMap heten; levert een naam niets
op, dan kost dat een zoekopdracht en gaat hij door met de volgende.

De lijst loopt ruwweg van groot naar klein. Grotere gemeenten leveren meer
bedrijven op, maar juist in kleinere zitten vaak de ondernemers zonder website.
"""

from __future__ import annotations

import random

GROTE_GEMEENTEN: list[str] = [
    "Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven", "Groningen",
    "Tilburg", "Almere", "Breda", "Nijmegen", "Apeldoorn", "Arnhem", "Haarlem",
    "Haarlemmermeer", "Amersfoort", "Enschede", "Zaanstad", "'s-Hertogenbosch",
    "Zwolle", "Zoetermeer", "Leiden", "Leeuwarden", "Ede", "Maastricht",
    "Dordrecht", "Westland", "Alphen aan den Rijn", "Alkmaar", "Emmen",
    "Delft", "Venlo", "Deventer", "Helmond", "Oss", "Hilversum", "Heerlen",
    "Amstelveen", "Súdwest-Fryslân", "Roosendaal", "Purmerend", "Schiedam",
    "Lelystad", "Almelo", "Hoorn", "Gouda", "Vlaardingen", "Assen", "Bergen op Zoom",
    "Capelle aan den IJssel", "Veenendaal", "Katwijk", "Zeist", "Nissewaard",
]

MIDDELGROTE_GEMEENTEN: list[str] = [
    "Hardenberg", "Oosterhout", "Hengelo", "Doetinchem", "Vlissingen", "Terneuzen",
    "Kampen", "Barneveld", "Woerden", "Rijswijk", "Ridderkerk", "Roermond",
    "Weert", "Sittard-Geleen", "Kerkrade", "Uden", "Veghel", "Waalwijk",
    "Zutphen", "Harderwijk", "Nieuwegein", "Houten", "IJsselstein", "Culemborg",
    "Tiel", "Wijchen", "Beuningen", "Duiven", "Zevenaar", "Rheden", "Epe",
    "Nunspeet", "Elburg", "Oldebroek", "Heerde", "Raalte", "Olst-Wijhe",
    "Dalfsen", "Ommen", "Staphorst", "Steenwijkerland", "Meppel", "Hoogeveen",
    "Coevorden", "Emmen", "Borger-Odoorn", "Aa en Hunze", "Tynaarlo", "Noordenveld",
    "Westerkwartier", "Midden-Groningen", "Veendam", "Stadskanaal", "Oldambt",
]

# Kleiner, vaak juist kansrijk: hier zit de ondernemer die het nooit
# geregeld heeft, maar er staan er ook minder van in OpenStreetMap.
KLEINE_GEMEENTEN: list[str] = [
    "Heerenveen", "Smallingerland", "Opsterland", "Ooststellingwerf",
    "Weststellingwerf", "De Fryske Marren", "Harlingen", "Waadhoeke",
    "Noardeast-Fryslân", "Dantumadiel", "Achtkarspelen", "Tytsjerksteradiel",
    "Urk", "Noordoostpolder", "Dronten", "Zeewolde", "Bunschoten", "Nijkerk",
    "Putten", "Ermelo", "Voorst", "Brummen", "Lochem", "Berkelland", "Winterswijk",
    "Aalten", "Oude IJsselstreek", "Montferland", "Bronckhorst", "Doesburg",
    "Westervoort", "Lingewaard", "Overbetuwe", "Neder-Betuwe", "Buren",
    "West Betuwe", "Zaltbommel", "Maasdriel", "Heusden", "Loon op Zand",
    "Dongen", "Gilze en Rijen", "Baarle-Nassau", "Alphen-Chaam", "Zundert",
    "Rucphen", "Etten-Leur", "Halderberge", "Moerdijk", "Steenbergen",
    "Woensdrecht", "Goes", "Middelburg", "Veere", "Schouwen-Duiveland",
    "Tholen", "Hulst", "Sluis", "Borsele", "Kapelle", "Reimerswaal",
]


GEMEENTEN: list[str] = GROTE_GEMEENTEN + MIDDELGROTE_GEMEENTEN + KLEINE_GEMEENTEN


def alle_gemeenten() -> list[str]:
    """Zonder dubbelen, in dezelfde volgorde."""
    gezien: set[str] = set()
    uniek: list[str] = []
    for naam in GEMEENTEN:
        if naam not in gezien:
            gezien.add(naam)
            uniek.append(naam)
    return uniek


def _ontdubbel(namen: list[str]) -> list[str]:
    gezien: set[str] = set()
    uniek: list[str] = []
    for naam in namen:
        if naam not in gezien:
            gezien.add(naam)
            uniek.append(naam)
    return uniek


def zoekvolgorde(door_elkaar: bool = True) -> list[str]:
    """De gemeenten in de volgorde waarin we ze afwerken.

    Puur loten klinkt eerlijk, maar levert weinig op: verreweg de meeste
    Nederlandse gemeenten zijn klein, dus een willekeurige greep is bijna altijd
    een dorp waar van een branche hooguit een of twee bedrijven in
    OpenStreetMap staan. Dan lijkt het alsof de machine niets vindt.

    Daarom mengen we: twee grote of middelgrote gemeenten, daarna een kleine.
    Binnen elke groep is de volgorde wel willekeurig, zodat twee keer starten
    niet twee keer dezelfde lijst geeft.
    """
    groot = _ontdubbel(GROTE_GEMEENTEN + MIDDELGROTE_GEMEENTEN)
    klein = [naam for naam in _ontdubbel(KLEINE_GEMEENTEN) if naam not in set(groot)]
    if door_elkaar:
        random.shuffle(groot)
        random.shuffle(klein)

    volgorde: list[str] = []
    i = j = 0
    while i < len(groot) or j < len(klein):
        for _ in range(2):
            if i < len(groot):
                volgorde.append(groot[i])
                i += 1
        if j < len(klein):
            volgorde.append(klein[j])
            j += 1
    return volgorde
