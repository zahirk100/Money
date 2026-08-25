# Lead-machine

Vindt lokale bedrijven met een zwakke online aanwezigheid, bouwt automatisch een
voorbeeldwebsite voor ze en zet de outreach klaar. Bedoeld om er zelf klanten mee
binnen te halen voor webdesign- of automatiseringswerk.

De machine doet het saaie werk: zoeken, beoordelen, bouwen en schrijven. Het
verkopen doe jij. Dat is bewust zo: outreach die volledig op de automaat gaat,
werkt niet meer en verbrandt alleen je domeinnaam.

## Hoe het werkt

```
discover  ->  audit  ->  demo  ->  wachtrij  ->  send
bedrijven     score      site      mail met      versturen
uit OSM       0-100      per lead  wachttijd     met limieten
```

Dat hele rijtje zit in een commando:

```bash
python -m leadmachine run          # een volledige cyclus
python -m leadmachine autopilot    # elke dag vanzelf
python -m leadmachine dashboard    # zien wat er gebeurt
```

Draaien kan op twee manieren, met dezelfde code:

- **Lokaal** - alles in een SQLite-bestand, dashboard op `127.0.0.1`.
- **Live** - Vercel voor de applicatie, Supabase voor de gegevens, Resend voor
  de mail, en een cron die dagelijks een stukje van de cyclus doet. Je krijgt
  een URL waar je inlogt en meekijkt. Zie **[DEPLOY.md](DEPLOY.md)**.

Welke van de twee het wordt, hangt af van een enkele omgevingsvariabele: staat
er een `DATABASE_URL`, dan praat hij met Postgres, anders met het bestand.

1. **discover** haalt bedrijven op uit OpenStreetMap via de Overpass API.
   Open data, geen scraping, geen voorwaarden die je schendt. Per gemeente een
   opdracht, met alle branches erin.
2. **audit** bezoekt hun website en kijkt of die deugt: bereikbaar, https,
   mobiel, laadtijd, titel, omschrijving, contactgegevens, hoe oud hij oogt.
   Elke bevinding levert punten op en een zin die je letterlijk kunt gebruiken.

   Staat er geen website bij het bedrijf, dan zoeken we er eerst zelf een. Dat
   moet: OpenStreetMap laat de website-tag bij verreweg de meeste bedrijven
   leeg, ook bij bedrijven met een prima site. Wie daarop filtert houdt niet de
   bedrijven zonder website over, maar de bedrijven die slecht zijn ingetekend.

   De regel die daaruit volgt geldt overal in deze code: **alleen wat de
   ondernemer zelf kan nakijken levert punten op.** Een site die een
   foutmelding geeft, niet op mobiel werkt of er zes seconden over doet - dat
   opent hij zelf en dan ziet hij het. Wat wij alleen maar vermoeden levert nul
   punten op en gaat nooit een mail in. Een ondernemer die je op een onwaarheid
   betrapt, leest je tweede mail niet meer.
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

## Automatisch draaien

```bash
python -m leadmachine autopilot          # blijft draaien, start elke dag om run_at
python -m leadmachine autopilot --once   # een keer draaien (voor cron of Taakplanner)
```

Zet in `config/campaign.yaml` onder `autopilot`:

```yaml
autopilot:
  enabled: true        # zonder dit doet de machine alles behalve versturen
  run_at: "09:15"
  mails_per_run: 20
  send_mode: review    # review = eerst in de wachtrij, auto = direct weg
  review_hours: 12
  min_score: 45
```

**`review` of `auto`.** In `review` belandt elke mail eerst in de wachtrij en
vertrekt hij pas na `review_hours`. Tot dat moment kun je hem in het dashboard
lezen, tegenhouden of juist meteen versturen. In `auto` gaat alles direct weg.

Begin met `review` en een lage `mails_per_run`. Eén fout in je tekst gaat er
anders vijfentwintig keer per dag uit, en je domeinnaam is binnen een week
verbrand bij de spamfilters. Zet `auto` pas aan als je een week lang hebt gezien
dat de mails kloppen.

Liever de planner van je besturingssysteem dan een proces dat blijft draaien:

```bash
# cron (Linux/macOS) - elke werkdag om 09:15
15 9 * * 1-5 cd /pad/naar/Money && PYTHONPATH=src python3 -m leadmachine autopilot --once >> out/autopilot.log 2>&1
```

## Het dashboard

```bash
python -m leadmachine dashboard        # http://127.0.0.1:8765
```

Vier tabbladen:

- **Overzicht** - hoeveel bedrijven gevonden, hoeveel kansrijk, hoeveel demo's,
  wat er in de wachtrij staat en wat er verstuurd is; een grafiek van de laatste
  veertien dagen, een trechter van vondst tot verstuurde mail, en het logboek van
  elke draaibeurt. De knop **Nu draaien** start een cyclus terwijl je meekijkt.
- **Leads** - alles op volgorde van score, met filters en zoeken. Per lead zie je
  wat er mis is, kun je de demo bekijken, een mail klaarzetten of het bedrijf op
  de afmeldlijst zetten.
- **Wachtrij** - wat er klaarstaat en wanneer het weggaat. Lezen, nu sturen of
  tegenhouden.
- **Verzonden** - alles wat de deur uit is, inclusief mislukte pogingen met de
  foutmelding erbij.

Het dashboard luistert alleen op `127.0.0.1` en schrijfacties vereisen een token
dat bij het starten wordt aangemaakt en alleen in de pagina zelf staat. Zo kan een
willekeurige website die je open hebt staan niet stiekem jouw lokale server
aansturen.

## Alle commando's

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

Alles offline uitproberen, zonder internet en zonder echte bedrijven - zet
`autopilot.source` op `fixture` en draai:

```bash
python -m leadmachine run --offline --dry-run
python -m leadmachine dashboard
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

- Versturen gebeurt alleen als `autopilot.enabled` op true staat of als je
  `--confirm` meegeeft. Standaard doet de machine alles behalve versturen.
- In `review` staat elke mail eerst in de wachtrij; vlak voor verzending wordt
  opnieuw op de afmeldlijst gecontroleerd.
- Harde dagelijkse limiet (standaard 25) en pauze tussen verzendingen.
- Afzendergegevens en een afmeldregel staan verplicht in elke mail.
- Wie op de afmeldlijst staat, wordt overgeslagen - op adres én op domein.
- Dezelfde onderneming niet opnieuw binnen de cooldown (standaard 90 dagen).
- De audit leest `robots.txt`, wacht tussen requests en identificeert zichzelf.
- Online blijft het dashboard dicht zolang er geen `DASHBOARD_PASSWORD` is
  ingesteld; de demopagina's blijven wel openbaar, want die moet je klant openen.

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
python tests/test_leadmachine.py            # audit, demo, mail, database
python tests/test_pipeline_dashboard.py     # cyclus, wachtrij, dashboard, toegang

# Draait dezelfde pijplijn op een echte Postgres. Slaat zichzelf over
# zonder database-URL. Wijs hem NOOIT naar je echte database: hij
# leegt de tabellen.
LM_TEST_DATABASE_URL=postgresql://... python tests/test_postgres.py
```

Vijfenveertig tests op SQLite en elf op Postgres, volledig offline: geen
netwerk, geen echte bedrijven, en er wordt nooit een mail verstuurd.
