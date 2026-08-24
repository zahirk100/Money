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
    # Persoonlijke verzorging
    "kapper": {"accent": "#b45309", "accent_donker": "#f59e0b", "tint": "#fef6ec",
               "sfeer": "Vakmanschap en rust", "motief": "schaar"},
    "schoonheidssalon": {"accent": "#be185d", "accent_donker": "#f472b6", "tint": "#fdf2f7",
                         "sfeer": "Verzorging met aandacht", "motief": "bloem"},
    "tattoo": {"accent": "#262626", "accent_donker": "#a3a3a3", "tint": "#f5f5f5",
               "sfeer": "Jouw idee, ons vakwerk", "motief": "naald"},
    # Bouw en techniek
    "aannemer": {"accent": "#c2410c", "accent_donker": "#fb923c", "tint": "#fff5ed",
                 "sfeer": "Afspraak is afspraak", "motief": "hamer"},
    "installateur": {"accent": "#0369a1", "accent_donker": "#38bdf8", "tint": "#eff8ff",
                     "sfeer": "Snel ter plaatse", "motief": "druppel"},
    "schilder": {"accent": "#4338ca", "accent_donker": "#818cf8", "tint": "#eef1ff",
                 "sfeer": "Strak afgewerkt", "motief": "kwast"},
    "dakdekker": {"accent": "#44403c", "accent_donker": "#a8a29e", "tint": "#f7f6f4",
                  "sfeer": "Droog en dicht", "motief": "dak"},
    "kozijnen": {"accent": "#0f766e", "accent_donker": "#2dd4bf", "tint": "#eefbf8",
                 "sfeer": "Licht binnen, stil binnen", "motief": "raam"},
    "meubelmaker": {"accent": "#854d0e", "accent_donker": "#eab308", "tint": "#fdf8ec",
                    "sfeer": "Op maat gemaakt", "motief": "planken"},
    # Auto en fiets
    "garage": {"accent": "#b91c1c", "accent_donker": "#f87171", "tint": "#fef4f4",
               "sfeer": "Eerlijk advies", "motief": "wiel"},
    "autohandel": {"accent": "#334155", "accent_donker": "#94a3b8", "tint": "#f3f5f8",
                   "sfeer": "Geen verrassingen achteraf", "motief": "auto"},
    "fietsenmaker": {"accent": "#047857", "accent_donker": "#34d399", "tint": "#edfbf5",
                     "sfeer": "Zo weer op de weg", "motief": "fiets"},
    "rijschool": {"accent": "#1d4ed8", "accent_donker": "#60a5fa", "tint": "#eef4ff",
                  "sfeer": "Rijden leer je met rust", "motief": "stuur"},
    # Groen
    "hovenier": {"accent": "#15803d", "accent_donker": "#4ade80", "tint": "#f1fdf4",
                 "sfeer": "Groen dat blijft staan", "motief": "blad"},
    # Horeca
    "restaurant": {"accent": "#9a3412", "accent_donker": "#fb923c", "tint": "#fff6ee",
                   "sfeer": "Lekker eten, zonder gedoe", "motief": "bord"},
    "cafe": {"accent": "#92400e", "accent_donker": "#fbbf24", "tint": "#fdf7ec",
             "sfeer": "Even zitten, even bijpraten", "motief": "kop"},
    "snackbar": {"accent": "#b45309", "accent_donker": "#fbbf24", "tint": "#fff8ea",
                 "sfeer": "Vers gebakken, snel klaar", "motief": "frites"},
    "catering": {"accent": "#6d28d9", "accent_donker": "#a78bfa", "tint": "#f5f1fe",
                 "sfeer": "Wij zorgen dat het klopt", "motief": "kok"},
    "hotel": {"accent": "#155e75", "accent_donker": "#22d3ee", "tint": "#edfaff",
              "sfeer": "Slapen op een fijne plek", "motief": "bed"},
    # Winkels met vakmanschap
    "bakker": {"accent": "#a16207", "accent_donker": "#facc15", "tint": "#fdf9ea",
               "sfeer": "Elke ochtend vers", "motief": "brood"},
    "slager": {"accent": "#be123c", "accent_donker": "#fb7185", "tint": "#fff1f3",
               "sfeer": "Kwaliteit die je proeft", "motief": "mes"},
    "delicatessen": {"accent": "#3f6212", "accent_donker": "#a3e635", "tint": "#f5fbe9",
                     "sfeer": "Met zorg uitgezocht", "motief": "tas"},
    "bloemist": {"accent": "#86198f", "accent_donker": "#e879f9", "tint": "#fdf2fd",
                 "sfeer": "Voor elk moment iets moois", "motief": "bloem"},
    "slijterij": {"accent": "#6b21a8", "accent_donker": "#c084fc", "tint": "#f8f2fe",
                  "sfeer": "Advies waar je iets aan hebt", "motief": "fles"},
    "kleding": {"accent": "#be185d", "accent_donker": "#f9a8d4", "tint": "#fdf3f8",
                "sfeer": "Kleding die echt past", "motief": "hanger"},
    "juwelier": {"accent": "#7c2d12", "accent_donker": "#fdba74", "tint": "#fff6f0",
                 "sfeer": "Voor de momenten die blijven", "motief": "ring"},
    "opticien": {"accent": "#1d4ed8", "accent_donker": "#93c5fd", "tint": "#eff5ff",
                 "sfeer": "Scherp zien, prettig dragen", "motief": "bril"},
    "computerzaak": {"accent": "#0369a1", "accent_donker": "#7dd3fc", "tint": "#eef8ff",
                     "sfeer": "Weer aan de praat", "motief": "laptop"},
    "keuken": {"accent": "#44403c", "accent_donker": "#d6d3d1", "tint": "#f7f6f5",
               "sfeer": "Van ontwerp tot plaatsing", "motief": "pan"},
    "dierenwinkel": {"accent": "#047857", "accent_donker": "#6ee7b7", "tint": "#eefbf5",
                     "sfeer": "Goed voor uw huisdier", "motief": "poot"},
    # Zorg
    "tandarts": {"accent": "#0e7490", "accent_donker": "#67e8f9", "tint": "#edfbff",
                 "sfeer": "Rustig behandeld", "motief": "tand"},
    "fysiotherapeut": {"accent": "#0f766e", "accent_donker": "#5eead4", "tint": "#eefbf9",
                       "sfeer": "Weer in beweging", "motief": "hart"},
    "dierenarts": {"accent": "#15803d", "accent_donker": "#86efac", "tint": "#f0fdf4",
                   "sfeer": "Zorg voor uw dier", "motief": "poot"},
    # Diensten
    "makelaar": {"accent": "#1e40af", "accent_donker": "#93c5fd", "tint": "#eff4ff",
                 "sfeer": "Thuis in de buurt", "motief": "sleutel"},
    "boekhouder": {"accent": "#334155", "accent_donker": "#cbd5e1", "tint": "#f4f6f9",
                   "sfeer": "Uw cijfers op orde", "motief": "rekenmachine"},
    "fotograaf": {"accent": "#262626", "accent_donker": "#d4d4d4", "tint": "#f6f6f6",
                  "sfeer": "Beelden die blijven", "motief": "camera"},
    "stomerij": {"accent": "#0369a1", "accent_donker": "#bae6fd", "tint": "#f0f9ff",
                 "sfeer": "Schoon en op tijd klaar", "motief": "wasmachine"},
    "uitvaart": {"accent": "#3f3f46", "accent_donker": "#d4d4d8", "tint": "#f7f7f8",
                 "sfeer": "Met aandacht geregeld", "motief": "kaars"},
    "sportschool": {"accent": "#b91c1c", "accent_donker": "#fca5a5", "tint": "#fff2f2",
                    "sfeer": "Trainen met begeleiding", "motief": "halter"},
    "reisbureau": {"accent": "#0f766e", "accent_donker": "#5eead4", "tint": "#effbf9",
                   "sfeer": "Van plan tot vertrek", "motief": "koffer"},
}
STANDAARD_THEMA = {"accent": "#1d4ed8", "accent_donker": "#60a5fa", "tint": "#eef4ff",
                   "sfeer": "Gewoon goed geregeld", "motief": "vink"}

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
    "brood": "M4.5 13a3.5 3 0 0 1 3.5-3h8a3.5 3 0 0 1 0 6H8a3.5 3 0 0 1-3.5-3Zm3-3V8.2m4.5 1.8V7.6m4.5 2.4V8.2",
    "mes": "M6 4h8l4 5-4 5H6V4Zm12 5h3M11 14v6M8.5 20h5",
    "fles": "M10 3h4v3.5l2 3V21H8V9.5l2-3V3Zm-2 8h8",
    "fiets": "M6 18.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm12 0a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM6 15l4.5-8h3.5l4 8M9.5 7h3.5",
    "bril": "M3 12h18M7.5 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Zm9 0a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
    "ring": "M12 9a5 5 0 1 0 0 10 5 5 0 0 0 0-10Zm0 0-3.5-4.5h7L12 9Z",
    "hanger": "M12 3.5a2 2 0 0 0-2 2c0 1.2 1 1.7 2 2v1.5L4 14.5V18h16v-3.5l-8-5.5",
    "tand": "M8 3.6c-2 0-3.5 1.6-3.5 4 0 3 1 5 1.7 8 .4 1.8 2.5 2 2.9 0L10 11h4l.9 4.6c.4 2 2.5 1.8 2.9 0 .7-3 1.7-5 1.7-8 0-2.4-1.5-4-3.5-4-1.6 0-2.4.9-4 .9s-2.4-.9-4-.9Z",
    "hart": "M12 20s-7-4.4-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.6-7 9-7 9Z",
    "poot": "M12 13.5c-2.5 0-4.5 2-4.5 4A2.5 2.5 0 0 0 10 20h4a2.5 2.5 0 0 0 2.5-2.5c0-2-2-4-4.5-4ZM7 10.6a1.8 2.2 0 1 0 0-4.4 1.8 2.2 0 0 0 0 4.4Zm10 0a1.8 2.2 0 1 0 0-4.4 1.8 2.2 0 0 0 0 4.4ZM4 15a1.6 1.9 0 1 0 0-3.8 1.6 1.9 0 0 0 0 3.8Zm16 0a1.6 1.9 0 1 0 0-3.8 1.6 1.9 0 0 0 0 3.8Z",
    "stuur": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 5a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm0 6v5M9.4 11 4.5 9.4m15 0L14.6 11",
    "rekenmachine": "M5 3.5h14v17H5v-17ZM7.5 7h9M8.5 11h.01M12 11h.01M15.5 11h.01M8.5 14.5h.01M12 14.5h.01M15.5 14.5h.01M8.5 18h.01M12 18h.01M15.5 18h.01",
    "halter": "M3 10v4m3-6v8m12-8v8m3-6v4M6 12h12",
    "bed": "M3 19v-9m0 5h18v4m0-4v-2a3 3 0 0 0-3-3h-7v5M6.6 11.6a1.9 1.9 0 1 0 0-3.8 1.9 1.9 0 0 0 0 3.8Z",
    "camera": "M4 8h3l1.5-2h7L17 8h3v11H4V8Zm8 3a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Z",
    "pan": "M4 10h13a4 4 0 0 1 0 8H8a4 4 0 0 1-4-4v-4Zm13 1.6h2.4a2.2 2.2 0 0 1 0 4.4H17M7.5 7.5V5m4 2.5V4m4 3.5V5",
    "planken": "M4 6.5h16v3.5H4V6.5Zm0 7h16V17H4v-3.5ZM8 10v3.5m8-3.5v3.5",
    "wasmachine": "M5 3.5h14v17H5v-17ZM12 9a4 4 0 1 0 0 8 4 4 0 0 0 0-8ZM8 6.5h.01M11 6.5h.01",
    "kaars": "M12 3s2.2 2.6 2.2 4.1a2.2 2.2 0 1 1-4.4 0C9.8 5.6 12 3 12 3Zm-3 8.5h6V21H9v-9.5Z",
    "laptop": "M5 6.5h14v9H5v-9ZM3 18.5h18M9.5 18.5h5",
    "naald": "M20.5 3.5 8 16l-3.5 4.5L9 17 21.5 4.5ZM6 14l4 4",
    "koffer": "M3.5 8h17v11h-17V8Zm5.5 0V5.5h6V8M3.5 13h17",
    "kop": "M4 7h12v6a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V7Zm12 1.5h2.5a2.5 2.5 0 0 1 0 5H16M7 4.5V3m3 1.5V3m3 1.5V3",
    "frites": "M8 9h8l-1 11H9L8 9Zm2-1V4m2 4V3m2 5V4.5",
    "dak": "M2.5 12.5 12 5.5l9.5 7M5 15h14M6.5 18h11M8 21h8",
    "raam": "M4 4h16v16H4V4Zm8 0v16M4 12h16",
    "mobiel": "M7.5 3h9v18h-9V3Zm3.5 15.5h2",
    "doos": "M3.5 7.5 12 4l8.5 3.5v9L12 20l-8.5-3.5v-9Zm0 0L12 11m0 0 8.5-3.5M12 11v9",
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
    "tattoo": [
        {"titel": "Ontwerp", "icoon": "naald",
         "tekst": "We tekenen je idee eerst uit, zodat je precies weet wat er komt."},
        {"titel": "Zetten", "icoon": "hand",
         "tekst": "Steriel werken, rustig tempo, en tussendoor pauze wanneer je dat wilt."},
        {"titel": "Nazorg", "icoon": "schild",
         "tekst": "Uitleg en producten mee, want de eerste twee weken bepalen het resultaat."},
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
    "schilder": [
        {"titel": "Binnen schilderwerk", "icoon": "kwast",
         "tekst": "Strak afgeplakt, netjes achtergelaten, en u kunt dezelfde dag weer wonen."},
        {"titel": "Buiten schilderwerk", "icoon": "huis",
         "tekst": "Eerst het hout herstellen, dan pas verven. Anders zit u er over twee jaar weer."},
        {"titel": "Wanden en stucwerk", "icoon": "kleur",
         "tekst": "Behang, spuitwerk of een gladde wand, met advies over kleur."},
    ],
    "dakdekker": [
        {"titel": "Lekkage opsporen", "icoon": "druppel",
         "tekst": "We zoeken waar het water echt binnenkomt, en niet waar het druppelt."},
        {"titel": "Dakbedekking", "icoon": "dak",
         "tekst": "Plat of schuin, nieuw of vervangen, met garantie op het werk."},
        {"titel": "Isolatie en onderhoud", "icoon": "schild",
         "tekst": "Een goed geisoleerd dak verdient zichzelf terug op de energierekening."},
    ],
    "kozijnen": [
        {"titel": "Kozijnen op maat", "icoon": "raam",
         "tekst": "Kunststof, hout of aluminium, ingemeten bij u thuis."},
        {"titel": "Glas en isolatie", "icoon": "zon",
         "tekst": "HR++ of triple glas: warmer in de winter, en merkbaar stiller."},
        {"titel": "Zonwering", "icoon": "wind",
         "tekst": "Screens, rolluiken of een zonnescherm, ook met bediening op afstand."},
    ],
    "meubelmaker": [
        {"titel": "Maatwerk", "icoon": "planken",
         "tekst": "Een kast of tafel die past waar niets standaards past."},
        {"titel": "Materiaal en afwerking", "icoon": "kleur",
         "tekst": "U kiest het hout en de afwerking, wij laten eerst een proefstukje zien."},
        {"titel": "Plaatsen", "icoon": "moersleutel",
         "tekst": "Wij brengen het en zetten het waterpas, zonder rommel achter te laten."},
    ],
    "garage": [
        {"titel": "APK", "icoon": "vink",
         "tekst": "Klaar terwijl u wacht, met eerlijk advies over wat echt moet en wat kan wachten."},
        {"titel": "Onderhoud en reparatie", "icoon": "moersleutel",
         "tekst": "Alle merken, altijd een prijsopgave voordat we beginnen."},
        {"titel": "Banden en seizoenswissel", "icoon": "wiel",
         "tekst": "Inclusief opslag van uw andere set, zodat u ze thuis niet kwijt kunt."},
    ],
    "autohandel": [
        {"titel": "Occasions", "icoon": "auto",
         "tekst": "Nagekeken voordat ze op de plaat staan, met de onderhoudshistorie erbij."},
        {"titel": "Inruil", "icoon": "kringloop",
         "tekst": "Uw huidige auto taxeren we ter plekke, ook als u niets koopt."},
        {"titel": "Garantie en aflevering", "icoon": "schild",
         "tekst": "Beurt, APK en tenaamstelling geregeld, u hoeft alleen te rijden."},
    ],
    "fietsenmaker": [
        {"titel": "Reparatie", "icoon": "moersleutel",
         "tekst": "Kleine klussen vaak dezelfde dag klaar, zodat je morgen weer kunt."},
        {"titel": "Onderhoudsbeurt", "icoon": "fiets",
         "tekst": "Remmen, versnellingen en verlichting nalopen, voor je de winter in gaat."},
        {"titel": "E-bike en accu", "icoon": "bliksem",
         "tekst": "Uitlezen van het systeem, accucheck en advies of vervangen echt nodig is."},
    ],
    "rijschool": [
        {"titel": "Rijlessen", "icoon": "stuur",
         "tekst": "In jouw tempo, met een vaste instructeur die je leert kennen."},
        {"titel": "Proefles", "icoon": "agenda",
         "tekst": "Eerst een les om te kijken of het klikt, daarna pas een pakket."},
        {"titel": "Examen", "icoon": "vink",
         "tekst": "Tussentijdse toets en examen regelen we, inclusief de auto die je gewend bent."},
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
    "cafe": [
        {"titel": "Koffie en lunch", "icoon": "kop",
         "tekst": "Van de eerste koffie tot een broodje tussen de middag."},
        {"titel": "Borrel", "icoon": "fles",
         "tekst": "Een plek waar je na het werk gewoon binnen kunt lopen."},
        {"titel": "Reserveren voor een groep", "icoon": "personen",
         "tekst": "Verjaardag, teamuitje of receptie: even overleggen wat er past."},
    ],
    "snackbar": [
        {"titel": "Vers gebakken", "icoon": "frites",
         "tekst": "Op bestelling klaargemaakt, dus even wachten en dan ook echt warm."},
        {"titel": "Afhalen", "icoon": "tas",
         "tekst": "Bellen kan, dan staat het klaar op het moment dat je er bent."},
        {"titel": "Bezorgen", "icoon": "bezorgen",
         "tekst": "Binnen het bezorggebied brengen we het thuis, warm verpakt."},
    ],
    "catering": [
        {"titel": "Buffet en borrelhapjes", "icoon": "bord",
         "tekst": "Van een eenvoudig buffet tot een verzorgd diner voor uw gasten."},
        {"titel": "Op locatie", "icoon": "pin",
         "tekst": "Wij komen naar u toe, met materiaal en personeel als dat nodig is."},
        {"titel": "Offerte op maat", "icoon": "munt",
         "tekst": "Zeg het aantal gasten en het budget, dan rekenen wij het uit."},
    ],
    "hotel": [
        {"titel": "Kamers", "icoon": "bed",
         "tekst": "Rustig slapen, met een ontbijt waar u de dag mee doorkomt."},
        {"titel": "Reserveren", "icoon": "agenda",
         "tekst": "Rechtstreeks boeken bij ons is voor u en voor ons het voordeligst."},
        {"titel": "In de buurt", "icoon": "kaart",
         "tekst": "We vertellen u graag waar u hier het beste kunt eten en wandelen."},
    ],
    "bakker": [
        {"titel": "Brood van eigen deeg", "icoon": "brood",
         "tekst": "'s Ochtends vroeg de oven in, zodat het bij openen nog warm is."},
        {"titel": "Gebak en taart", "icoon": "kleur",
         "tekst": "Voor verjaardagen en feesten, met naam of foto erop als je dat wilt."},
        {"titel": "Bestellen", "icoon": "agenda",
         "tekst": "Bel een dag van tevoren, dan leggen we het voor je apart."},
    ],
    "slager": [
        {"titel": "Vers vlees", "icoon": "mes",
         "tekst": "Zelf gesneden, en we vertellen er graag bij waar het vandaan komt."},
        {"titel": "Eigen worst en salades", "icoon": "bord",
         "tekst": "Huisgemaakt, volgens recepten die we niet zomaar veranderen."},
        {"titel": "Bestellen voor het weekend", "icoon": "agenda",
         "tekst": "Barbecue of gourmet: geef het aantal personen door, wij regelen de rest."},
    ],
    "delicatessen": [
        {"titel": "Uitgezocht assortiment", "icoon": "tas",
         "tekst": "Niet het grootste schap, wel producten waar we zelf achter staan."},
        {"titel": "Advies", "icoon": "hand",
         "tekst": "Vertel waarvoor het is, dan zoeken we er iets bij dat past."},
        {"titel": "Cadeaupakket", "icoon": "doos",
         "tekst": "Samengesteld naar budget, netjes ingepakt om weg te geven."},
    ],
    "bloemist": [
        {"titel": "Boeketten", "icoon": "bloem",
         "tekst": "Gebonden op het moment zelf, met bloemen die op dat moment mooi zijn."},
        {"titel": "Bruiloft en uitvaart", "icoon": "hart",
         "tekst": "Persoonlijk werk, waar we rustig met u over doorpraten."},
        {"titel": "Bezorgen", "icoon": "bezorgen",
         "tekst": "In de omgeving brengen we het dezelfde dag rond."},
    ],
    "slijterij": [
        {"titel": "Advies", "icoon": "hand",
         "tekst": "Zeg waar je het bij drinkt, dan zoeken we er iets bij."},
        {"titel": "Bijzondere flessen", "icoon": "fles",
         "tekst": "Ook kleinere stokerijen en brouwerijen die je niet overal vindt."},
        {"titel": "Cadeau en proeverij", "icoon": "doos",
         "tekst": "Ingepakt meegeven, of een proeverij voor een groep."},
    ],
    "kleding": [
        {"titel": "Collectie", "icoon": "hanger",
         "tekst": "Met zorg ingekocht, zodat je niet in dezelfde jas rondloopt als de rest."},
        {"titel": "Persoonlijk advies", "icoon": "spiegel",
         "tekst": "We kijken mee in de pashokjes, en zeggen ook als iets niet staat."},
        {"titel": "Vermaken", "icoon": "schaar",
         "tekst": "Broek korter of taille innemen: dat regelen we hier."},
    ],
    "juwelier": [
        {"titel": "Sieraden", "icoon": "ring",
         "tekst": "Voor een verjaardag, een jubileum of gewoon omdat het mooi is."},
        {"titel": "Reparatie en taxatie", "icoon": "moersleutel",
         "tekst": "Vermaken, polijsten of laten schatten voor de verzekering."},
        {"titel": "Trouwringen", "icoon": "hart",
         "tekst": "Rustig uitzoeken op afspraak, met de winkel even voor u alleen."},
    ],
    "opticien": [
        {"titel": "Oogmeting", "icoon": "bril",
         "tekst": "Uitgebreid gemeten, met de tijd om het rustig uit te leggen."},
        {"titel": "Brillen en lenzen", "icoon": "spiegel",
         "tekst": "Monturen passen zonder haast, en meekijken wat bij uw gezicht past."},
        {"titel": "Service", "icoon": "moersleutel",
         "tekst": "Bijstellen, schoonmaken en kleine reparaties, ook als u hem elders kocht."},
    ],
    "computerzaak": [
        {"titel": "Reparatie", "icoon": "laptop",
         "tekst": "Scherm, accu of trage computer: eerst kijken, dan een prijs, dan pas maken."},
        {"titel": "Telefoons", "icoon": "mobiel",
         "tekst": "Schermen en accu's vaak dezelfde dag vervangen."},
        {"titel": "Overzetten en back-up", "icoon": "kringloop",
         "tekst": "Uw bestanden en foto's mee naar het nieuwe toestel, zonder dat er iets kwijtraakt."},
    ],
    "keuken": [
        {"titel": "Ontwerp", "icoon": "pan",
         "tekst": "We meten in en tekenen het uit, zodat u ziet hoe het echt wordt."},
        {"titel": "Apparatuur", "icoon": "bliksem",
         "tekst": "Advies over wat u werkelijk gebruikt, in plaats van de duurste optie."},
        {"titel": "Plaatsing", "icoon": "moersleutel",
         "tekst": "Inclusief water, elektra en afvoer, door mensen die het vaker doen."},
    ],
    "dierenwinkel": [
        {"titel": "Voer en advies", "icoon": "poot",
         "tekst": "Wat past bij de leeftijd en het gewicht van uw dier, zonder onzin."},
        {"titel": "Verzorging", "icoon": "hand",
         "tekst": "Trimmen, nagels knippen en producten voor thuis."},
        {"titel": "Bestellen", "icoon": "doos",
         "tekst": "Vaste merken leggen we voor u klaar, zodat u niet misgrijpt."},
    ],
    "tandarts": [
        {"titel": "Controle", "icoon": "tand",
         "tekst": "Twee keer per jaar, met uitleg over wat we zien en wat het betekent."},
        {"titel": "Behandeling", "icoon": "schild",
         "tekst": "Vullingen, kronen en wortelkanaal, met aandacht voor mensen die opzien tegen de stoel."},
        {"titel": "Mondhygiene", "icoon": "hand",
         "tekst": "Reiniging en advies waarmee u thuis verder kunt."},
    ],
    "fysiotherapeut": [
        {"titel": "Behandeling", "icoon": "hart",
         "tekst": "Eerst uitzoeken waar de klacht vandaan komt, dan pas behandelen."},
        {"titel": "Oefeningen voor thuis", "icoon": "halter",
         "tekst": "Een kort schema dat haalbaar is naast werk en gezin."},
        {"titel": "Afspraak", "icoon": "agenda",
         "tekst": "Meestal kunt u zonder verwijzing terecht, ook 's avonds."},
    ],
    "dierenarts": [
        {"titel": "Spreekuur", "icoon": "poot",
         "tekst": "Op afspraak, met de tijd om uw vragen echt te beantwoorden."},
        {"titel": "Vaccinatie en chip", "icoon": "schild",
         "tekst": "Inclusief paspoort, ook voor als u met uw dier op reis gaat."},
        {"titel": "Spoed", "icoon": "telefoon",
         "tekst": "Bel eerst, dan weten we dat u eraan komt en staan we klaar."},
    ],
    "makelaar": [
        {"titel": "Verkoop", "icoon": "sleutel",
         "tekst": "Van waardebepaling tot sleuteloverdracht, met een vast aanspreekpunt."},
        {"titel": "Aankoop", "icoon": "huis",
         "tekst": "Wij kijken mee, ook naar wat er op de foto's niet te zien is."},
        {"titel": "Gratis waardebepaling", "icoon": "munt",
         "tekst": "Weten wat uw huis waard is, zonder dat u iets hoeft te beslissen."},
    ],
    "boekhouder": [
        {"titel": "Boekhouding", "icoon": "rekenmachine",
         "tekst": "Uw administratie bijgehouden, zodat u weet hoe u ervoor staat."},
        {"titel": "Aangiftes", "icoon": "agenda",
         "tekst": "Btw en inkomstenbelasting op tijd ingediend, zonder herinneringen."},
        {"titel": "Advies", "icoon": "hand",
         "tekst": "Even bellen over een investering of een contract mag gewoon."},
    ],
    "fotograaf": [
        {"titel": "Reportage", "icoon": "camera",
         "tekst": "Bruiloft, gezin of bedrijf: beelden waar u over tien jaar nog blij mee bent."},
        {"titel": "Portret", "icoon": "persoon",
         "tekst": "In de studio of buiten, met de tijd om te wennen aan de camera."},
        {"titel": "Nabewerking", "icoon": "kleur",
         "tekst": "Een selectie zorgvuldig bewerkt, en digitaal aangeleverd."},
    ],
    "stomerij": [
        {"titel": "Stomen en wassen", "icoon": "wasmachine",
         "tekst": "Pakken, jassen en gordijnen, behandeld naar wat het materiaal aankan."},
        {"titel": "Reparatie en vermaak", "icoon": "schaar",
         "tekst": "Ritsen, zomen en knopen, terwijl het toch hier is."},
        {"titel": "Klaar wanneer afgesproken", "icoon": "klok",
         "tekst": "U hoort direct wanneer het klaar is, en dan is het ook klaar."},
    ],
    "uitvaart": [
        {"titel": "Begeleiding", "icoon": "kaars",
         "tekst": "Wij nemen het regelwerk over, u houdt de ruimte om erbij stil te staan."},
        {"titel": "Persoonlijk afscheid", "icoon": "hart",
         "tekst": "Zoals het bij hem of haar paste, ook als dat niet het gebruikelijke is."},
        {"titel": "Vooraf regelen", "icoon": "agenda",
         "tekst": "Een gesprek nu scheelt uw nabestaanden later veel vragen."},
    ],
    "sportschool": [
        {"titel": "Trainen", "icoon": "halter",
         "tekst": "Ruime openingstijden, zodat het naast werk en gezin past."},
        {"titel": "Begeleiding", "icoon": "persoon",
         "tekst": "Een schema dat bij je doel past, en iemand die meekijkt op de vloer."},
        {"titel": "Proeftraining", "icoon": "vink",
         "tekst": "Eerst een keer meedoen en rondkijken, daarna pas beslissen."},
    ],
    "reisbureau": [
        {"titel": "Reis op maat", "icoon": "koffer",
         "tekst": "Vertel wat voor vakantie u zoekt, dan zoeken wij het uit."},
        {"titel": "Alles geregeld", "icoon": "vlag",
         "tekst": "Vlucht, verblijf en vervoer op elkaar afgestemd, met een reisschema erbij."},
        {"titel": "Bereikbaar tijdens de reis", "icoon": "telefoon",
         "tekst": "Gaat er onderweg iets mis, dan heeft u iemand die u kunt bellen."},
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


# Een regel onder "Waar we voor klaarstaan". Die stond eerst tegen de ondernemer
# te praten ("een vaste plek waar klanten zien wat u doet"), terwijl de pagina
# juist moet lezen als de site van het bedrijf zelf.
ONDERKOPPEN = {
    "kapper": "Van een snelle knipbeurt tot een compleet nieuwe kleur.",
    "schoonheidssalon": "Behandelingen waar je de rest van de week wat aan hebt.",
    "tattoo": "Van eerste schets tot uitgewerkte tatoeage.",
    "aannemer": "Grote verbouwingen en kleine klussen, met dezelfde afspraken.",
    "installateur": "Storingen, nieuwe installaties en het onderhoud daartussenin.",
    "schilder": "Binnen en buiten, met voorbereiding die het werk laat zitten.",
    "dakdekker": "Van een lekkage opsporen tot een compleet nieuw dak.",
    "kozijnen": "Kozijnen, glas en zonwering, ingemeten en geplaatst.",
    "meubelmaker": "Gemaakt in eigen werkplaats, op de maat die u nodig hebt.",
    "garage": "APK, onderhoud en reparatie voor alle merken.",
    "autohandel": "Occasions, inruil en de papieren die erbij horen.",
    "fietsenmaker": "Reparatie, onderhoud en alles rond de e-bike.",
    "rijschool": "Van proefles tot het examen, met dezelfde instructeur.",
    "hovenier": "Ontwerp, aanleg en onderhoud van uw tuin.",
    "restaurant": "Wat er bij ons op tafel komt en hoe u een tafel vastlegt.",
    "cafe": "Koffie overdag, borrel aan het eind van de dag.",
    "snackbar": "Vers gebakken, om mee te nemen of te laten bezorgen.",
    "catering": "Van borrelhapjes tot een compleet verzorgd diner.",
    "hotel": "Kamers, ontbijt en tips voor wat u hier kunt doen.",
    "bakker": "Brood uit eigen oven, gebak voor als er iets te vieren is.",
    "slager": "Vers vlees, eigen worst en alles voor het weekend.",
    "delicatessen": "Producten die we zelf hebben geproefd voordat ze in de kast staan.",
    "bloemist": "Boeketten, bloemwerk en bezorging in de omgeving.",
    "slijterij": "Een uitgezocht assortiment, met advies erbij.",
    "kleding": "Kleding, advies in het pashokje en vermaken als het net niet past.",
    "juwelier": "Sieraden, trouwringen en reparaties in eigen atelier.",
    "opticien": "Oogmeting, monturen en service zolang u de bril draagt.",
    "computerzaak": "Reparatie, advies en het overzetten van uw gegevens.",
    "keuken": "Van het eerste ontwerp tot de laatste kraan.",
    "dierenwinkel": "Voer, verzorging en advies voor uw huisdier.",
    "tandarts": "Controle, behandeling en mondhygiene onder een dak.",
    "fysiotherapeut": "Behandeling in de praktijk en oefeningen voor thuis.",
    "dierenarts": "Spreekuur, vaccinaties en hulp wanneer het spoed is.",
    "makelaar": "Verkopen, aankopen en weten wat uw huis waard is.",
    "boekhouder": "Uw administratie, uw aangiftes en advies als u erom vraagt.",
    "fotograaf": "Reportages, portretten en beelden voor uw bedrijf.",
    "stomerij": "Stomen, wassen en kleine reparaties, op tijd klaar.",
    "uitvaart": "Wij regelen het, u houdt de ruimte om afscheid te nemen.",
    "sportschool": "Trainen wanneer het u uitkomt, met iemand die meekijkt.",
    "reisbureau": "Een reis die past, van het eerste idee tot de terugvlucht.",
}
STANDAARD_ONDERKOP = "Dit is waar u bij ons voor terechtkunt."


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value) or "bedrijf"


# Verandert het ontwerp, dan hoort dit nummer op te lopen. De cyclus bouwt
# demo's met een ouder nummer opnieuw, zodat een verbetering ook terechtkomt
# bij bedrijven waarvoor al eerder een pagina is gemaakt.
DEMO_VERSIE = "4"


def kaart_embed(lat: float | None, lon: float | None) -> str | None:
    """Een uitsnede van OpenStreetMap rond het bedrijf.

    Dit is het detail waarop een ondernemer zijn eigen straat herkent, en dat
    scheelt in een voorstel meer dan nog een mooie kleur."""
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    # Ongeveer 400 bij 250 meter: straatniveau, zonder dat het huisnummer zoek raakt.
    dx, dy = 0.0032, 0.0014
    return (
        "https://www.openstreetmap.org/export/embed.html"
        f"?bbox={lon - dx:.5f}%2C{lat - dy:.5f}%2C{lon + dx:.5f}%2C{lat + dy:.5f}"
        f"&layer=mapnik&marker={lat:.5f}%2C{lon:.5f}"
    )


def thema_van(niche: str) -> dict[str, str]:
    return THEMAS.get(niche, STANDAARD_THEMA)


KOPPEN = {
    "kapper": "Goed geknipt, zonder lang wachten",
    "schoonheidssalon": "Even tijd voor uzelf, vlakbij huis",
    "tattoo": "Jouw idee, zorgvuldig gezet",
    "aannemer": "Vakwerk dat af is wanneer we het beloven",
    "installateur": "Snel geholpen bij storing en onderhoud",
    "schilder": "Strak schilderwerk, netjes achtergelaten",
    "dakdekker": "Uw dak weer dicht, zonder gedoe",
    "kozijnen": "Nieuwe kozijnen: warmer, stiller, lichter",
    "meubelmaker": "Meubels op maat, gemaakt om te blijven",
    "garage": "Uw auto in vertrouwde handen",
    "autohandel": "Een auto kopen zonder verrassingen achteraf",
    "fietsenmaker": "Zo weer op de weg",
    "rijschool": "Je rijbewijs halen in je eigen tempo",
    "hovenier": "Een tuin waar u het hele jaar van geniet",
    "restaurant": "Lekker eten, zonder gedoe",
    "cafe": "Even zitten en bijpraten",
    "snackbar": "Vers gebakken, snel klaar",
    "catering": "Wij zorgen dat het eten klopt",
    "hotel": "Een fijne plek om te overnachten",
    "bakker": "Elke ochtend vers uit eigen oven",
    "slager": "Vers vlees, gesneden zoals u het wilt",
    "delicatessen": "Met zorg uitgezocht, dat proef je",
    "bloemist": "Voor elk moment iets moois",
    "slijterij": "Advies waar je iets aan hebt",
    "kleding": "Kleding die echt past",
    "juwelier": "Voor de momenten die blijven",
    "opticien": "Scherp zien, prettig dragen",
    "computerzaak": "Weer aan de praat, vaak dezelfde dag",
    "keuken": "Van ontwerp tot geplaatste keuken",
    "dierenwinkel": "Goed voor uw huisdier, eerlijk advies",
    "tandarts": "Rustig behandeld, alles uitgelegd",
    "fysiotherapeut": "Weer in beweging, stap voor stap",
    "dierenarts": "Zorg voor uw dier, met de tijd erbij",
    "makelaar": "Thuis in de buurt, thuis in uw huis",
    "boekhouder": "Uw cijfers op orde, uw hoofd vrij",
    "fotograaf": "Beelden waar u over tien jaar nog blij mee bent",
    "stomerij": "Schoon en klaar wanneer afgesproken",
    "uitvaart": "Een afscheid dat past, met aandacht geregeld",
    "sportschool": "Trainen met begeleiding, in uw eigen tempo",
    "reisbureau": "Van plan tot vertrek, alles geregeld",
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
        f"{naam} is een {niche_label} in {waar}. Bel gerust of loop binnen; "
        "we vertellen u graag wat we voor u kunnen doen."
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
    # Meestal staat dit als kolom op de lead; valt terug op de ruwe tags, zodat
    # een pagina nooit zonder openingstijden staat terwijl OSM ze wel heeft.
    tijden = get("opening_hours") or tags.get("opening_hours") or ""
    week = parse_opening_hours(tijden)

    return template.render(
        lead={
            "naam": naam, "stad": stad, "postcode": get("postcode"),
            "telefoon": telefoon, "email": get("email"),
        },
        niche_label=niche_label,
        thema=thema_van(niche),
        kop=_kop(niche, niche_label, stad),
        onderkop=ONDERKOPPEN.get(niche, STANDAARD_ONDERKOP),
        intro=_intro(naam, niche_label, stad, tags),
        meta_omschrijving=(
            f"{naam} - {niche_label}{' in ' + stad if stad else ''}. "
            "Openingstijden, diensten en contactgegevens op een rij."
        ),
        diensten=DIENSTEN.get(niche, STANDAARD_DIENSTEN),
        iconen=ICONEN,
        week=week,
        openingstijden_tekst=tijden,
        feiten=feiten_uit_tags(tags),
        socials=sociale_links(tags),
        adres=adres,
        route_url=(
            f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
            if lat and lon else None
        ),
        kaart_url=kaart_embed(lat, lon),
        telefoon_href=re.sub(r"[^\d+]", "", telefoon),
        afzender=campaign.outreach,
        jaar=date.today().year,
    )
