"""Nederlandse gemeenten om automatisch uit te putten.

In de automatische stand trekt de machine zelf combinaties van gemeente en
branche uit deze lijst, zodat je niet zelf hoeft te bedenken waar je gaat
zoeken. De namen staan zoals ze in OpenStreetMap heten; levert een naam niets
op, dan kost dat een zoekopdracht en gaat hij door met de volgende.

De lijst loopt ruwweg van groot naar klein. Grotere gemeenten leveren meer
bedrijven op, maar juist in kleinere zitten vaak de ondernemers zonder website.
"""

from __future__ import annotations

GEMEENTEN: list[str] = [
    # Grote steden
    "Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven", "Groningen",
    "Tilburg", "Almere", "Breda", "Nijmegen", "Apeldoorn", "Arnhem", "Haarlem",
    "Haarlemmermeer", "Amersfoort", "Enschede", "Zaanstad", "'s-Hertogenbosch",
    "Zwolle", "Zoetermeer", "Leiden", "Leeuwarden", "Ede", "Maastricht",
    "Dordrecht", "Westland", "Alphen aan den Rijn", "Alkmaar", "Emmen",
    "Delft", "Venlo", "Deventer", "Helmond", "Oss", "Hilversum", "Heerlen",
    "Amstelveen", "Súdwest-Fryslân", "Roosendaal", "Purmerend", "Schiedam",
    "Lelystad", "Almelo", "Hoorn", "Gouda", "Vlaardingen", "Assen", "Bergen op Zoom",
    "Capelle aan den IJssel", "Veenendaal", "Katwijk", "Zeist", "Nissewaard",
    # Middelgroot
    "Hardenberg", "Oosterhout", "Hengelo", "Doetinchem", "Vlissingen", "Terneuzen",
    "Kampen", "Barneveld", "Woerden", "Rijswijk", "Ridderkerk", "Roermond",
    "Weert", "Sittard-Geleen", "Kerkrade", "Uden", "Veghel", "Waalwijk",
    "Zutphen", "Harderwijk", "Nieuwegein", "Houten", "IJsselstein", "Culemborg",
    "Tiel", "Wijchen", "Beuningen", "Duiven", "Zevenaar", "Rheden", "Epe",
    "Nunspeet", "Elburg", "Oldebroek", "Heerde", "Raalte", "Olst-Wijhe",
    "Dalfsen", "Ommen", "Staphorst", "Steenwijkerland", "Meppel", "Hoogeveen",
    "Coevorden", "Emmen", "Borger-Odoorn", "Aa en Hunze", "Tynaarlo", "Noordenveld",
    "Westerkwartier", "Midden-Groningen", "Veendam", "Stadskanaal", "Oldambt",
    # Kleiner, vaak juist kansrijk
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


def alle_gemeenten() -> list[str]:
    """Zonder dubbelen, in dezelfde volgorde."""
    gezien: set[str] = set()
    uniek: list[str] = []
    for naam in GEMEENTEN:
        if naam not in gezien:
            gezien.add(naam)
            uniek.append(naam)
    return uniek
