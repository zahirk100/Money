"""De volledige cyclus in een keer: zoeken, beoordelen, bouwen, opstellen, versturen.

Dit is wat de autopilot draait, wat de knop in het dashboard aanroept en wat de
cron van Vercel elke keer een stukje van doet. Vandaar het tijdsbudget: in een
serverless omgeving mag een aanroep maar een beperkt aantal seconden duren, dus
elke stap kijkt op de klok en stopt op tijd. De volgende aanroep pakt op waar
deze ophield, want de voortgang staat in de database.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from . import ai_tekst
from . import db as database
from .audit import audit_lead
from .config import Campaign, load_dotenv
from .demo import DEMO_VERSIE, build_demo
from .discover import discover
from .gemeenten import zoekvolgorde
from .http import PoliteClient
from .outreach import Mailer, OutreachError, draft_email, eligible, load_suppression_file
from .store import Store, klok, now, stamp

Reporter = Callable[[str], None]

# Achterwaartse compatibiliteit: het dashboard importeerde deze naam.
_stamp = staticmethod(stamp) if False else (lambda moment: stamp(moment))


# Hoe lang we het ophalen laten rusten na een storing bij Overpass.
PAUZE_MINUTEN = 20


def _noop(_: str) -> None:
    pass


def _kort(exc: Exception) -> str:
    """Een leesbare samenvatting voor in het dashboard.

    Eerder knipte dit alles weg vanaf het eerste haakje. Dat gooide net het
    stuk weg waar je iets aan hebt ("te veel verzoeken (429)"), en liet een
    melding over waar niemand mee verder kon.
    """
    tekst = " ".join(str(exc).split()) or type(exc).__name__
    return tekst[:220]


class Budget:
    """Bewaakt hoeveel tijd een aanroep nog heeft. Zonder budget: onbeperkt."""

    def __init__(self, seconds: float | None) -> None:
        self.seconds = seconds
        self.started = time.monotonic()

    @property
    def left(self) -> float:
        return float("inf") if not self.seconds else self.seconds - (time.monotonic() - self.started)

    def ok(self, reserve: float = 0.0) -> bool:
        return self.left > reserve

    def allows(self, reserve: float, gedaan: int) -> bool:
        """Elke stap doet er minstens een per aanroep. Anders zou een budget dat
        kleiner is dan de reservering betekenen dat er nooit iets gebeurt, en
        blijft de machine stilstaan zonder dat iemand ziet waarom."""
        return gedaan == 0 or self.left > reserve


def autopilot_settings(campaign: Campaign) -> dict[str, Any]:
    raw = campaign._raw.get("autopilot") or {}

    def value(key: str, fallback: Any) -> Any:
        """Instellingen mogen ook uit de omgeving komen, zodat je ze op Vercel
        kunt aanpassen zonder de code opnieuw uit te rollen."""
        env = os.environ.get("LM_" + key.upper())
        return env if env not in (None, "") else raw.get(key, fallback)

    return {
        "enabled": str(value("enabled", False)).lower() in {"true", "1", "yes", "ja"},
        "run_at": str(value("run_at", "09:15")),
        "discover_every_days": int(value("discover_every_days", 7)),
        "audits_per_run": int(value("audits_per_run", 200)),
        "demos_per_run": int(value("demos_per_run", 25)),
        "mails_per_run": int(value("mails_per_run", 20)),
        "send_mode": str(value("send_mode", "review")),
        "review_hours": int(value("review_hours", 12)),
        "min_score": int(value("min_score", 45)),
        "backlog_grens": int(value("backlog_grens", 150)),
        "source": str(value("source", "overpass")),
        # Ook bedrijven met een website ophalen. Het klinkt logisch om die over
        # te slaan, maar OpenStreetMap laat de website-tag bij verreweg de
        # meeste bedrijven leeg - ook bij bedrijven met een prima site. Filter
        # je daarop, dan hou je niet de bedrijven zonder website over maar de
        # bedrijven die slecht zijn ingetekend, en beweert je mail iets wat de
        # ontvanger meteen kan weerleggen. Een site die aantoonbaar stuk, traag
        # of niet mobiel is, is bovendien een betere aanleiding dan een
        # vermoeden. Zet op true als je toch alleen de lege wilt.
        "alleen_zonder_website": str(value("alleen_zonder_website", False)).lower()
                                 in {"true", "1", "yes", "ja"},
    }


def public_base_url() -> str:
    for key in ("PUBLIC_BASE_URL", "DEMO_BASE_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value if value.startswith("http") else f"https://{value}"
    return ""


def demo_url_for(slug: str) -> str | None:
    base = public_base_url().rstrip("/")
    return f"{base}/demo/{slug}" if base else None


def _writes_to_disk() -> bool:
    """Op Vercel is alleen /tmp schrijfbaar, dus daar slaan we niets op schijf op."""
    return not os.environ.get("VERCEL")


def _should_discover(store: Store, every_days: int) -> bool:
    last = database.get_meta(store, "last_discover")
    if not last:
        return True
    try:
        return now() - database._as_datetime(last) >= timedelta(days=every_days)
    except (TypeError, ValueError):
        return True


def _discover_step(
    campaign: Campaign,
    store: Store,
    settings: dict[str, Any],
    budget: Budget,
    report: Reporter,
    force: bool,
) -> int:
    """Haalt bedrijven op, een gemeente per keer.

    Een enkele Overpass-query duurt zomaar tien tot dertig seconden, en een
    handvol daarvan loopt ver over wat een serverless functie mag draaien.
    Daarom wordt na elke gemeente opgeslagen wat er nog open staat: de volgende
    aanroep pakt de rest op in plaats van weer vooraan te beginnen.
    """
    # Ligt er nog een flinke stapel te beoordelen, dan is die stapel meer waard
    # dan nog meer bedrijven erbij. Maar alleen bedrijven met een website kosten
    # tijd: daar moet een pagina voor opgehaald worden. Voor de rest is het
    # oordeel meteen klaar, en die horen het ophalen dus niet tegen te houden.
    wachtend = int(store.scalar(
        "SELECT COUNT(*) AS n FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
        "WHERE a.id IS NULL AND l.website IS NOT NULL AND l.website <> ''"
    ) or 0)
    if not force and wachtend >= int(settings.get("backlog_grens", 40)):
        report(f"Ophalen overgeslagen: eerst {wachtend} websites beoordelen.")
        return 0

    # Na een mislukte poging even niet opnieuw: Overpass is dan meestal druk,
    # en elke poging kost het budget dat het beoordelen nodig heeft.
    pauze = database.get_meta(store, "discover_pauze_tot")
    tot = database._as_datetime(pauze) if pauze else None
    if not force and tot and tot > now():
        # Zeggen tot wanneer, en dat de rest gewoon doorgaat. "Even op pauze"
        # zonder tijd erbij laat je op de knop blijven drukken.
        reden = database.get_meta(store, "discover_pauze_reden") or ""
        report(
            f"Ophalen staat op pauze tot {klok(tot)}"
            + (f" - {reden}" if reden else " na een eerdere storing")
            + ". Beoordelen en demo's bouwen gaan wel door."
        )
        return 0

    # Waar we zoeken: per gemeente, alle branches in een opdracht. Zie
    # build_query_gebied waarom dat niet per branche gaat.
    if campaign.automatisch:
        # Zelf kiezen: alle gemeenten uit de lijst, door elkaar. De volgorde
        # ligt vast zodra hij is bepaald, zodat een volgende aanroep verdergaat
        # in plaats van opnieuw te loten.
        dekking = zoekvolgorde()
        vingerafdruk = f"auto:{len(dekking)}gemeenten:{len(campaign.niches)}branches"
    else:
        dekking = list(campaign.areas)
        vingerafdruk = "|".join(sorted(dekking)) + f":{len(campaign.niches)}branches"
    gewijzigd = database.get_meta(store, "discover_dekking") != vingerafdruk

    openstaand = json.loads(database.get_meta(store, "discover_pending") or "[]")
    if gewijzigd:
        # Een gemeente of branche erbij (of eraf) betekent opnieuw langs alles.
        # Zonder deze controle zou hij pas over een week merken dat er iets is
        # veranderd, en tot die tijd dezelfde stad blijven doen.
        nog_niet_gedaan = [plek for plek in dekking if plek not in set(openstaand)]
        openstaand = openstaand + nog_niet_gedaan if openstaand else list(dekking)
        # Alleen gemeenten die we nu nog willen.
        openstaand = [plek for plek in openstaand if plek in set(dekking)]
        database.set_meta(store, "discover_dekking", vingerafdruk)
        database.set_meta(store, "discover_pending", json.dumps(openstaand))
        store.commit()
        report(f"Zoekgebied gewijzigd: {len(openstaand)} gemeenten te doen.")
    elif not openstaand:
        if not (force or _should_discover(store, settings["discover_every_days"])):
            return 0
        openstaand = list(dekking)

    gevonden = 0
    gedaan = 0
    # Een opdracht per gemeente haalt alle branches tegelijk op, dus een handvol
    # gemeenten per beurt dekt al veel. De reservering is afgestemd op een
    # gewone query van een paar seconden.
    while openstaand and gedaan < MAX_ZOEKOPDRACHTEN and budget.allows(12, gedaan):
        gebied = openstaand[0] or campaign.area
        report(f"Bedrijven ophalen in {gebied}...")
        # Nooit langer wachten dan er nog tijd is: anders kapt het platform de
        # functie af terwijl wij nog netjes hadden kunnen opslaan.
        wachttijd = 30.0 if budget.left == float("inf") else max(8.0, budget.left - 8)
        try:
            binnen = list(discover(
                campaign, source=settings["source"],
                timeout=wachttijd, area=gebied,
                alleen_zonder_website=settings.get("alleen_zonder_website", True),
            ))
        except Exception as exc:  # noqa: BLE001
            # Een storing bij Overpass mag de rest van de cyclus niet slopen:
            # beoordelen, demo's bouwen en versturen hebben er niets mee te
            # maken. De gemeente blijft openstaan voor de volgende beurt.
            database.set_meta(
                store, "discover_pauze_tot", stamp(now() + timedelta(minutes=PAUZE_MINUTEN))
            )
            database.set_meta(store, "discover_pauze_reden", _kort(exc))
            store.commit()
            report(
                f"Ophalen in {gebied} lukte niet: {_kort(exc)}. "
                f"Volgende {PAUZE_MINUTEN} minuten geen zoekopdrachten; "
                "de rest van de cyclus gaat door."
            )
            return gevonden
        nieuw = database.upsert_many(store, binnen)
        gevonden += nieuw
        # Erbij zeggen welke branches het waren: staat er twee keer achter
        # elkaar niets, dan wil je kunnen zien of het aan de gemeente ligt of
        # aan de lijst met tags.
        soorten = Counter(lead["niche"] for lead in binnen)
        samenvatting = ", ".join(f"{aantal}x {naam}" for naam, aantal in soorten.most_common(6))
        report(
            f"{gebied}: {len(binnen)} gevonden, {nieuw} nieuw"
            + (f" ({samenvatting})" if samenvatting else " - hier staat niets in OpenStreetMap")
            + "."
        )
        openstaand.pop(0)
        gedaan += 1
        database.set_meta(store, "discover_pending", json.dumps(openstaand))
        database.set_meta(store, "discover_pauze_tot", "")
        database.set_meta(store, "discover_pauze_reden", "")
        store.commit()

    if openstaand:
        report(f"{gevonden} nieuw; nog {len(openstaand)} gemeenten te gaan, volgende beurt verder.")
    else:
        database.set_meta(store, "last_discover", stamp())
        database.set_meta(store, "discover_pending", "[]")
        store.commit()
        if gevonden:
            report(f"{gevonden} nieuwe bedrijven gevonden.")
    return gevonden


# Hoeveel websites tegelijk worden opgehaald. Wachten op een trage server is
# stilstaan; met een paar tegelijk gaat er per beurt veel meer doorheen. Elk
# bedrijf heeft zijn eigen server, dus dit belast niemand extra: per host blijft
# het netjes een verzoek tegelijk met pauze ertussen.
AUDIT_WORKERS = int(os.environ.get("LM_AUDIT_WORKERS", "6"))

# Hoeveel zoekopdrachten een beurt hoogstens doet. Meer dan dit laat geen tijd
# over om de gevonden bedrijven ook te beoordelen.
MAX_ZOEKOPDRACHTEN = int(os.environ.get("LM_ZOEKOPDRACHTEN_PER_BEURT", "6"))

# Hoeveel teksten tegelijk geschreven worden. Dit wacht alleen op de API, dus
# meer tegelijk kost geen extra rekenkracht bij ons.
TEKST_WORKERS = int(os.environ.get("LM_TEKST_WORKERS", "5"))

_draad_eigen = threading.local()


def _client_van_deze_draad(campaign: Campaign) -> PoliteClient:
    if not hasattr(_draad_eigen, "client"):
        _draad_eigen.client = PoliteClient(
            user_agent=str(campaign.audit.get("user_agent", "LeadMachine/1.0")),
            timeout=float(campaign.audit.get("timeout_seconds", 12)),
            delay=float(campaign.audit.get("delay_seconds", 1.5)),
            respect_robots=bool(campaign.audit.get("respect_robots", True)),
        )
    return _draad_eigen.client


def _om_en_om(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Branches om en om, in plaats van eerst alle kappers.

    Beoordelen gebeurt op volgorde van binnenkomst, en het ophalen gaat branche
    voor branche. Zonder deze verdeling staat je hele lijst vol met de branche
    die toevallig als eerste aan de beurt was.
    """
    per_branche: dict[str, list[dict[str, Any]]] = {}
    for lead in leads:
        per_branche.setdefault(lead.get("niche") or "", []).append(lead)

    verdeeld: list[dict[str, Any]] = []
    rijen = list(per_branche.values())
    for stand in range(max((len(rij) for rij in rijen), default=0)):
        for rij in rijen:
            if stand < len(rij):
                verdeeld.append(rij[stand])
    return verdeeld


def _audit_step(
    campaign: Campaign,
    store: Store,
    todo: list[dict[str, Any]],
    budget: Budget,
    report: Reporter,
    offline: bool,
) -> int:
    """Beoordeelt websites met een paar tegelijk, en slaat op per groepje."""
    gedaan = 0
    alsnog = 0

    def beoordeel(lead: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        client = None if offline else _client_van_deze_draad(campaign)
        return lead, audit_lead(lead, client=client, offline=offline)

    todo = _om_en_om(todo)
    groep = 1 if offline else AUDIT_WORKERS
    with ThreadPoolExecutor(max_workers=groep) as pool:
        for start in range(0, len(todo), groep):
            if not budget.allows(18, gedaan):
                report("Tijd op; de volgende beurt gaat verder met beoordelen.")
                break
            for lead, resultaat in pool.map(beoordeel, todo[start : start + groep]):
                resultaat["segment"] = campaign.segment(resultaat["score"])
                # Zelf een site gevonden die niet in OpenStreetMap stond? Die
                # hoort bij de lead, zodat we hem later niet nog eens zoeken en
                # in het dashboard te zien is waar het oordeel over gaat.
                if resultaat.get("website_gevonden"):
                    database.set_lead_website(store, lead["id"], resultaat["website_gevonden"])
                    alsnog += 1
                database.save_audit(store, lead["id"], resultaat)
                gedaan += 1
            store.commit()

    if gedaan:
        report(
            f"{gedaan} websites beoordeeld"
            + (f", waarvan {alsnog} met een site die niet in OpenStreetMap stond." if alsnog else ".")
        )
    return gedaan


def _demo_doelversie() -> str:
    """Met AI-teksten aan is een pagina iets anders dan zonder. Door dat in het
    versienummer te zetten worden bestaande pagina's een keer opnieuw gebouwd
    zodra je de AI aanzet - en niet elke beurt opnieuw."""
    if ai_tekst.ingeschakeld():
        return f"{DEMO_VERSIE}+ai{ai_tekst.TEKST_VERSIE}"
    return DEMO_VERSIE


def _tekst_cache_sleutel(lead_id: int) -> str:
    return f"aitekst:{lead_id}"


def _teksten_ophalen(
    campaign: Campaign, store: Store, rijen: list[dict[str, Any]],
    budget: "Budget", report: Any,
) -> dict[int, dict[str, Any]]:
    """Per bedrijf een eigen tekst, uit de cache of nieuw geschreven.

    Eenmaal geschreven blijft een tekst staan, ook als het ontwerp verandert:
    anders betaal je bij elke ontwerpwijziging opnieuw voor dezelfde woorden.
    """
    uit: dict[int, dict[str, Any]] = {}
    if not rijen:
        return uit

    nodig = []
    for rij in rijen:
        bewaard = database.get_meta(store, _tekst_cache_sleutel(rij["id"]))
        if bewaard:
            try:
                pakket = json.loads(bewaard)
            except ValueError:
                pakket = {}
            if pakket.get("versie") == ai_tekst.TEKST_VERSIE and pakket.get("tekst"):
                uit[rij["id"]] = pakket["tekst"]
                continue
        nodig.append(rij)

    if not nodig or not ai_tekst.ingeschakeld():
        return uit

    def schrijf(rij: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        niche_obj = campaign.niche(rij.get("niche") or "")
        tags = rij.get("raw") or {}
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = {}
        wacht = 25.0 if budget.left == float("inf") else max(8.0, budget.left - 12)
        return rij["id"], ai_tekst.tekst_voor(
            rij, niche_obj.label if niche_obj else "bedrijf", tags, timeout=wacht
        )

    report(f"Teksten schrijven voor {len(nodig)} bedrijven...")
    mislukt = 0
    with ThreadPoolExecutor(max_workers=TEKST_WORKERS) as pool:
        for lead_id, tekst in pool.map(schrijf, nodig):
            if tekst:
                uit[lead_id] = tekst
                database.set_meta(store, _tekst_cache_sleutel(lead_id), json.dumps(
                    {"versie": ai_tekst.TEKST_VERSIE, "model": ai_tekst.model(), "tekst": tekst},
                    ensure_ascii=False,
                ))
            else:
                mislukt += 1
    store.commit()
    if mislukt:
        report(
            f"Voor {mislukt} bedrijven lukte het schrijven niet; die pagina's "
            "krijgen de vaste tekst en worden een volgende beurt opnieuw geprobeerd."
        )
    return uit


def run_cycle(
    campaign: Campaign,
    store: Store,
    trigger: str = "handmatig",
    live: bool | None = None,
    report: Reporter = _noop,
    force_discover: bool = False,
    offline: bool = False,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Draait een cyclus. live=None betekent: volg de instellingen."""
    load_dotenv()
    settings = autopilot_settings(campaign)
    if live is None:
        live = settings["enabled"]
    budget = Budget(budget_seconds)

    # Alles wat de cyclus meldt gaat ook de database in. Anders zie je achteraf
    # alleen een rij met nullen en niet waarom het nullen waren - en juist dat
    # wil je weten als er iets niet loopt.
    regels: list[str] = []

    def meld(bericht: str) -> None:
        regels.append(str(bericht))
        report(bericht)

    counters = {"discovered": 0, "audited": 0, "demos": 0, "queued": 0, "sent": 0, "failed": 0}
    database.close_stale_runs(store)
    run_id = database.start_run(store, trigger)

    try:
        load_suppression_file(store, campaign)

        # 1. Nieuwe bedrijven ophalen, branche voor branche.
        counters["discovered"] = _discover_step(
            campaign, store, settings, budget, meld, force_discover
        )

        # 2. Websites beoordelen, zolang er tijd is.
        todo = database.leads_without_audit(store, settings["audits_per_run"])
        # Wat we eerder niet konden bereiken, verdient een tweede kans.
        ruimte = settings["audits_per_run"] - len(todo)
        if ruimte > 0:
            todo += database.leads_needing_recheck(store, limit=ruimte)
        if todo:
            meld(f"{len(todo)} websites te beoordelen...")
            counters["audited"] = _audit_step(campaign, store, todo, budget, meld, offline)

        # 3. Voorbeeldsites bouwen voor de beste leads die er nog geen hebben.
        # Nog geen demo, of een demo van voor de laatste ontwerpwijziging.
        doelversie = _demo_doelversie()
        candidates = database.leads_needing_demo(
            store,
            limit=settings["demos_per_run"],
            min_score=settings["min_score"],
            versie=doelversie,
        )
        # Teksten laten schrijven duurt per bedrijf een paar seconden; naast
        # elkaar scheelt dat het verschil tussen drie en vijftien pagina's per
        # beurt. Staat de AI uit, dan gebeurt hier niets.
        teksten = _teksten_ophalen(campaign, store, candidates, budget, meld)
        for row in candidates:
            eigen_tekst = teksten.get(row["id"])
            # Een beurt zonder AI-tekst hoort ruim binnen de tijd te passen; met
            # tekst is het meeste werk al gedaan voordat we hier zijn.
            if not budget.allows(12, counters["demos"]):
                break
            slug, html = build_demo(row, campaign, eigen_tekst)
            path = None
            if _writes_to_disk():
                from .demo import render_demo

                path = str(render_demo(row, campaign, tekst=eigen_tekst))
            database.record_demo(
                store, row["id"], slug, path=path, url=demo_url_for(slug),
                html=html,
                # Lukte de tekst niet, dan noteren we de pagina als 'gewone'
                # versie. Dan probeert hij het een volgende beurt opnieuw in
                # plaats van met een half resultaat te blijven staan.
                versie=doelversie if (eigen_tekst or not ai_tekst.ingeschakeld()) else DEMO_VERSIE,
            )
            counters["demos"] += 1
        if counters["demos"]:
            store.commit()
            met_tekst = sum(1 for row in candidates[: counters["demos"]] if teksten.get(row["id"]))
            meld(
                f"{counters['demos']} voorbeeldsites gebouwd"
                + (f", waarvan {met_tekst} met eigen tekst." if ai_tekst.ingeschakeld() else ".")
            )

        # 4. Mails opstellen en in de wachtrij zetten.
        wait_hours = 0 if settings["send_mode"] == "auto" else settings["review_hours"]
        send_after = stamp(now() + timedelta(hours=wait_hours))
        for row in database.ranked_leads(store, limit=settings["mails_per_run"] * 4, with_email=True):
            if counters["queued"] >= settings["mails_per_run"]:
                break
            if not budget.allows(10, counters["queued"]):
                break
            if (row["score"] or 0) < settings["min_score"]:
                continue
            if database.already_queued(store, row["id"]):
                continue
            ok, _reason = eligible(store, row, campaign)
            if not ok:
                continue
            message = draft_email(row, campaign, demo_url=row.get("demo_url") or demo_url_for(row.get("demo_slug") or ""))
            database.queue_outreach(store, row["id"], message, "first", send_after)
            counters["queued"] += 1
        if counters["queued"]:
            store.commit()
            meld(
                f"{counters['queued']} mails klaargezet"
                + (f" (gaan over {wait_hours} uur de deur uit)." if wait_hours else ".")
            )

        # 5. Versturen wat aan de beurt is.
        counters["sent"], counters["failed"] = send_due(
            campaign, store, live=live, report=meld, budget=budget
        )

        database.bewaar_runlog(store, run_id, regels)
        database.finish_run(store, run_id, counters)
        store.commit()
        return counters

    except Exception as exc:  # noqa: BLE001 - een mislukte run mag de autopilot niet slopen
        meld(f"Fout tijdens de cyclus: {exc}")
        database.bewaar_runlog(store, run_id, regels)
        database.finish_run(store, run_id, counters, status="mislukt", error=str(exc))
        store.commit()
        traceback.print_exc()
        raise


def send_due(
    campaign: Campaign,
    store: Store,
    live: bool = False,
    report: Reporter = _noop,
    limit: int | None = None,
    budget: Budget | None = None,
) -> tuple[int, int]:
    """Verstuurt wachtrij-items waarvan de wachttijd voorbij is."""
    budget = budget or Budget(None)
    daily_limit = int(campaign.outreach.get("daily_limit", 25))
    room = max(0, daily_limit - database.sent_today(store))
    budget_count = min(room, limit) if limit else room
    if budget_count <= 0:
        report("Dagelijkse limiet bereikt; vandaag niets meer versturen.")
        return 0, 0

    due = database.due_outreach(store, budget_count)
    if not due:
        return 0, 0

    if not live:
        report(f"{len(due)} mails staan klaar, maar versturen staat uit (proefdraai).")
        return 0, 0

    # Vlak voor verzending nogmaals toetsen: er kan intussen een afmelding
    # binnengekomen zijn. Dit gebeurt voordat de verbinding opengaat.
    queue = []
    for item in due:
        if database.is_suppressed(store, item["to_addr"] or ""):
            database.mark_outreach(store, item["id"], "geannuleerd", "afmeldlijst")
            report(f"overgeslagen: {item['to_addr']} staat op de afmeldlijst")
        else:
            queue.append(item)
    store.commit()
    if not queue:
        return 0, 0

    pause = float(campaign.outreach.get("seconds_between_sends", 45))
    sent = failed = 0
    try:
        with Mailer(campaign, live=True) as mailer:
            for index, item in enumerate(queue):
                try:
                    mailer.send(item["to_addr"], item["subject"], item["body"])
                    database.mark_outreach(store, item["id"], "verstuurd")
                    sent += 1
                    report(f"verstuurd naar {item['to_addr']} ({item['lead_name']})")
                except Exception as exc:  # noqa: BLE001
                    database.mark_outreach(store, item["id"], "mislukt", str(exc))
                    failed += 1
                    report(f"mislukt naar {item['to_addr']}: {exc}")
                store.commit()
                if index < len(queue) - 1:
                    if not budget.ok(reserve=pause + 5):
                        report("Tijd op; de rest gaat de volgende beurt de deur uit.")
                        break
                    time.sleep(pause)
    except OutreachError as exc:
        report(str(exc))
        return sent, failed
    return sent, failed
