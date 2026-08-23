# Lead-machine

Vindt lokale bedrijven met een zwakke online aanwezigheid, bouwt automatisch een
voorbeeldwebsite voor ze en zet de outreach klaar. Bedoeld om er zelf klanten mee
binnen te halen voor webdesign- of automatiseringswerk.

De machine doet het saaie werk: zoeken, beoordelen, bouwen en schrijven. Het
verkopen doe jij. Dat is bewust zo: outreach die volledig op de automaat gaat,
werkt niet meer en verbrandt alleen je domeinnaam.

## Hoe het werkt

```
discover  ->  audit  ->  demo  ->  draft/calllist  ->  send
bedrijven     score      site      mail of belllijst   versturen
uit OSM       0-100      per lead  met openingszin     met limieten
```

1. **discover** haalt bedrijven op uit OpenStreetMap via de Overpass API.
   Open data, geen scraping, geen voorwaarden die je schendt. Juist de
   ondernemers die zelf niets aan hun online aanwezigheid doen staan daar
   zonder website-tag - dat is precies je doelgroep.
2. **audit** kijkt of er een site is en of die deugt: bereikbaar, https,
   mobiel, laadtijd, titel, omschrijving, contactgegevens, hoe oud hij oogt.
   Elke bevinding levert punten op en een zin die je letterlijk kunt gebruiken.
3. **demo** genereert een complete voorbeeldpagina met hun naam, adres,
   telefoonnummer en openingstijden erin. Dit is je verkoopargument: niet
   "ik kan iets voor u maken", maar "het staat er al".
4. **draft** schrijft de mail, **calllist** maakt een belllijst met
   gespreksopeningen per bedrijf.
5. **send** verstuurt, met dagelijkse limiet, pauzes, afmeldlijst en cooldown.

## Installeren

```bash
pip install -r requirements.txt
cp config/campaign.example.yaml config/campaign.yaml
cp .env.example .env
export PYTHONPATH=src
```

Vul in `config/campaign.yaml` je regio, je branches en je afzendergegevens in.
Zonder naam, adres en mailadres weigert de tool te versturen.

## Gebruik

```bash
python -m leadmachine init
python -m leadmachine discover                 # bedrijven ophalen
python -m leadmachine audit                    # websites beoordelen
python -m leadmachine list -v --segment hot    # wat is er te halen
python -m leadmachine demo --top 25            # voorbeeldsites bouwen
python -m leadmachine calllist --top 25        # belllijst voor morgen
python -m leadmachine draft --top 20           # mails als concept
python -m leadmachine send --limit 10          # proefdraai
python -m leadmachine send --limit 10 --confirm  # echt versturen
python -m leadmachine stats                    # stand van de pijplijn
python -m leadmachine suppress info@bedrijf.nl # afmelding verwerken
```

Alles offline uitproberen, zonder internet en zonder echte bedrijven:

```bash
python -m leadmachine discover --source fixture
python -m leadmachine audit --offline
```

De demo's komen in `out/demos/`, de conceptmails in `out/outreach/`, de
belllijst in `out/`. Alles staat in `data/leads.db`.

## Demo's publiceren

De demo's zijn losse mappen met een `index.html` en geen enkel extern bestand.
Zet `out/demos/` op GitHub Pages, Netlify of Cloudflare Pages, en zet de basis-URL
in `.env` als `DEMO_BASE_URL`. Die link komt dan automatisch in je mails en op je
belllijst te staan.

Elke demo draagt bovenaan een balk dat het een vrijblijvend voorbeeld is en geen
officiële website van het bedrijf, en staat op `noindex`. Laat dat staan: het
voorkomt verwarring, het voorkomt dat je pagina met hun naam in Google komt, en
het is precies de reden dat ondernemers er ontspannen op reageren.

## Grenzen die in de code zitten

Niet als advies in een handleiding, maar afgedwongen:

- `send` verstuurt niets zonder `--confirm`; standaard is elke run een proefdraai.
- Harde dagelijkse limiet (standaard 25) en pauze tussen verzendingen.
- Afzendergegevens en een afmeldregel staan verplicht in elke mail.
- Wie op de afmeldlijst staat, wordt overgeslagen - op adres én op domein.
- Dezelfde onderneming niet opnieuw binnen de cooldown (standaard 90 dagen).
- De audit leest `robots.txt`, wacht tussen requests en identificeert zichzelf.

Verder, wat je zelf moet weten: je benadert bedrijven, geen consumenten. Gebruik
alleen mailadressen die het bedrijf zelf openbaar heeft gemaakt, verwerk een
afmelding dezelfde dag, en bel liever dan dat je een tweede mail stuurt. Zakelijk
bellen mag - het bel-me-niet-register geldt alleen voor consumenten.

## Wat werkt in de praktijk

- **Bellen converteert vele malen beter dan mailen.** De mail is het excuus voor
  het telefoontje ("ik stuurde u vorige week een voorbeeldpagina"). Gebruik de
  belllijst als hoofdkanaal en mail als opwarmer.
- **Eén branche tegelijk.** Tien kappers bellen gaat veel beter dan tien
  willekeurige bedrijven: je leert hun bezwaren en je tweede gesprek is al beter.
- **Vraag geen ja op de website, vraag ja op twee minuten.** De demo doet het werk.
- **Prijs vast, scope vast.** Vanaf-prijs in de mail, exacte prijs in het gesprek.
- Reken op grofweg 1 klant per 20-30 goed gevoerde gesprekken in het begin.
  Bij 750 euro per site is dat een reële start; het echte geld zit in
  onderhoudsabonnementen daarna (bijv. 25-50 euro per maand per klant).

## Uitbreidingen die het meeste opleveren

1. Onderhoudsabonnement met een maandelijkse factuur - terugkerende omzet.
2. Google Bedrijfsprofiel aanmaken en bijhouden als losse dienst.
3. Reviews automatisch opvragen na een klus (hoge waarde, weinig werk).
4. Tweede audit een maand later: wie z'n site inmiddels heeft aangepast, is warm.

## Tests

```bash
python tests/test_leadmachine.py       # of: python -m pytest tests -q
```

Draait volledig offline: geen netwerk, geen echte database, geen echte bedrijven.
