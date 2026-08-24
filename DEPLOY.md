# Live zetten op Vercel en Supabase

Je krijgt hiermee een URL waar jij inlogt en alles ziet, terwijl een cron elke
dag zelf de cyclus draait. Reken op drie kwartier de eerste keer.

Wat waar draait:

| Onderdeel | Waar | Waarom daar |
|---|---|---|
| Dashboard en API | Vercel (Python-functie) | een URL, geen server om te beheren |
| Gegevens | Supabase (Postgres) | serverless functies hebben geen eigen schijf |
| Demopagina's | Supabase, geserveerd door Vercel | openbare link die je in je mail zet |
| Dagelijkse cyclus | Vercel Cron | geen proces dat blijft draaien |
| Mail versturen | Resend | SMTP werkt slecht vanuit serverless |

---

## 1. Supabase: de database

1. Maak een project op [supabase.com](https://supabase.com). Kies een regio in
   Europa (Frankfurt), dat scheelt latency en houdt de gegevens in de EU.
2. Klik bovenin op **Connect** (het stekkertje naast de projectnaam) en kies
   onder *Connection string* de **Session pooler** (poort 5432). Hij ziet
   eruit als:

   ```
   postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-1-eu-central-1.pooler.supabase.com:5432/postgres
   ```

   Vervang `[YOUR-PASSWORD]` door het databasewachtwoord dat je bij het
   aanmaken hebt gekozen. Kwijt? **Project Settings > Database > Reset
   database password**.

   Niet de *Direct connection* nemen: die is alleen over IPv6 bereikbaar en
   daar kan Vercel niet bij. De *Transaction pooler* (6543) werkt ook - de
   code zet prepared statements uit, juist daarvoor - maar session pooling is
   voor dit werk de rustigste keuze.

   > Deze string bevat je databasewachtwoord. Plak hem alleen in Vercel, niet
   > in een chat, een issue of een commit.
3. Optioneel: open de **SQL Editor** en draai `supabase/schema.sql`. Hoeft niet
   - de applicatie maakt de tabellen zelf aan bij de eerste start - maar zo zie
   je precies wat er staat.

> De applicatie praat rechtstreeks met Postgres, niet via de Supabase-API. Je
> hebt dus geen anon-sleutel of service-key nodig; alleen de connection string.

## 2. Resend: mail versturen

1. Maak een account op [resend.com](https://resend.com) en voeg je **eigen
   domein** toe.
2. Zet de DNS-records die Resend je geeft (SPF, DKIM, en DMARC). Zonder een
   geverifieerd domein kun je alleen naar jezelf mailen.
3. Maak een API-sleutel aan.

Mail je vanaf een gratis adres of een niet-geverifieerd domein, dan komt vrijwel
alles in de spammap. Een domein kost een tientje per jaar; dit is niet de plek
om dat uit te sparen.

## 3. Vercel: de applicatie

1. Push deze repository naar GitHub en importeer hem op
   [vercel.com](https://vercel.com). Framework preset: **Other**. Er is geen
   buildstap; `vercel.json` regelt de rest.
2. Zet onder **Settings > Environment Variables** het volgende:

| Variabele | Waarde | Verplicht |
|---|---|---|
| `DATABASE_URL` | de connection string uit stap 1 | ja |
| `DASHBOARD_PASSWORD` | wachtwoord waarmee jij inlogt | ja |
| `SESSION_SECRET` | lange willekeurige tekst | ja |
| `DASHBOARD_TOKEN` | lange willekeurige tekst | ja |
| `CRON_SECRET` | lange willekeurige tekst | ja |
| `PUBLIC_BASE_URL` | `https://jouwproject.vercel.app` | ja |
| `RESEND_API_KEY` | de sleutel uit stap 2 | om te mailen |
| `LM_SENDER_NAME`, `LM_SENDER_EMAIL`, `LM_COMPANY_NAME`, `LM_COMPANY_ADDRESS`, `LM_KVK`, `LM_PHONE` | je afzendergegevens | om te mailen |
| `LM_AREA` | `auto` (zelf gemeenten kiezen) of eigen gemeenten met komma's | nee |
| `LM_ENABLED` | `false` om te beginnen | nee |

   Drie geheimen genereren:

   ```bash
   python3 -c "import secrets; [print(secrets.token_urlsafe(32)) for _ in range(3)]"
   ```

3. Deploy. Ga naar `https://jouwproject.vercel.app` - je krijgt een inlogscherm.

> Mis je nog iets, dan krijg je geen kale foutmelding maar een pagina die
> opnoemt welke variabelen ontbreken. `https://jouwproject.vercel.app/gezond`
> geeft hetzelfde als JSON: `{"status":"ok","klaar":true}` betekent dat alles
> staat. Let op: een variabele toevoegen werkt pas na een nieuwe uitrol
> (Deployments &rsaquo; ... &rsaquo; Redeploy).

Zonder `DASHBOARD_PASSWORD` blijft het dashboard dicht met een melding. Dat is
expres: anders staat je hele leadbestand inclusief verzendknop open op internet.
De demopagina's blijven wel openbaar, want die moeten je klanten kunnen openen.

## 4. De eerste cyclus

Log in en druk op **Nu draaien**. De eerste keer haalt hij de bedrijven op uit
OpenStreetMap; daarna beoordeelt hij websites, bouwt demo's en zet mails klaar.

Loopt de functie tegen de tijdslimiet aan, dan stopt hij netjes en gaat de
volgende beurt verder waar deze ophield. De voortgang staat in de database, dus
je kunt gerust meerdere keren op de knop drukken.

## 5. Automatisch laten draaien

De cron staat in `vercel.json` op elke dag 08:00 UTC:

```json
"crons": [{ "path": "/api/cron", "schedule": "0 8 * * *" }]
```

Op het gratis Hobby-plan mag een cron **een keer per dag** draaien. Wil je vaker
(bijvoorbeeld elk uur, zodat de wachtrij sneller leegloopt), dan heb je een
Pro-abonnement nodig. Het eindpunt is daarop gebouwd: elke aanroep doet een
stukje.

Alles draait in een functie: het dashboard, de API, de demopagina's en de cron.
Vercel wil een entrypoint dat het statisch kan vinden, en dat staat in
`pyproject.toml`:

```toml
[tool.vercel]
entrypoint = "api.index:handler"
```

Zet daarna `LM_ENABLED=true`. Pas dan gaat er echt mail de deur uit.

Zelf een cyclus starten zonder te wachten:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://jouwproject.vercel.app/api/cron
```

## 6. Voordat je echt gaat mailen

- Zet `LM_SEND_MODE=review` en `LM_MAILS_PER_RUN=5`. Kijk een week lang in het
  tabblad **Wachtrij** of de mails kloppen voordat je ze laat vertrekken.
- Stuur de eerste vijf naar jezelf: zet je eigen adres bij een testbedrijf in de
  database en kijk hoe de mail binnenkomt.
- Controleer een demopagina op je telefoon. Dat is waar je klant hem opent.
- Pas als dat een week goed gaat: `LM_MAILS_PER_RUN` omhoog, en eventueel
  `LM_SEND_MODE=auto`.

## Wat het kost

| | Gratis | Wanneer je moet betalen |
|---|---|---|
| Vercel | Hobby: 1 cron per dag | Pro (20 dollar/mnd) voor vaker draaien |
| Supabase | 500 MB database | ruim voldoende voor tienduizenden leads |
| Resend | 3.000 mails per maand | daarboven vanaf 20 dollar/mnd |
| Domein | - | ongeveer 10 euro per jaar |

Je kunt dus gratis beginnen. De eerste rekening die je tegenkomt is het domein.

## Problemen oplossen

| Wat je ziet | Wat er aan de hand is |
|---|---|
| Pagina "Bijna klaar - er ontbreekt nog wat" | de opgesomde variabelen staan nog niet op Vercel, of je hebt na het invullen nog niet opnieuw uitgerold |
| Inloggen lukt, knoppen doen niets | `DASHBOARD_TOKEN` ontbreekt, elke instantie verzint dan een eigen token |
| `/api/cron` geeft 401 | `CRON_SECRET` ontbreekt of komt niet overeen |
| Cyclus stopt halverwege | normaal: tijdsbudget op, de volgende beurt gaat verder |
| Mails blijven op "In wachtrij" staan | `LM_ENABLED` staat op false, of de wachttijd is nog niet voorbij |
| Resend weigert de mail | domein nog niet geverifieerd, of afzenderadres hoort niet bij dat domein |
| Overpass geeft een fout | de gratis API is druk; hij probeert twee servers, morgen lukt het wel |

## Lokaal blijven draaien

Alles blijft werken zonder Vercel of Supabase. Zonder `DATABASE_URL` gebruikt
hij gewoon `data/leads.db`:

```bash
export PYTHONPATH=src
python -m leadmachine run --offline --dry-run
python -m leadmachine dashboard
```

Je kunt ook lokaal tegen je Supabase-database werken door `DATABASE_URL` in je
`.env` te zetten. Dan zie je op je eigen machine precies dezelfde gegevens als
op de live URL.
