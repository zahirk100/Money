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
from concurrent.futures import ThreadPoolExecutor
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db as database
from .audit import audit_lead
from .config import Campaign, load_dotenv
from .demo import DEMO_VERSIE, build_demo
from .discover import discover
from .http import PoliteClient
from .outreach import Mailer, OutreachError, draft_email, eligible, load_suppression_file
from .store import Store, now, stamp

Reporter = Callable[[str], None]

# Achterwaartse compatibiliteit: het dashboard importeerde deze naam.
_stamp = staticmethod(stamp) if False else (lambda moment: stamp(moment))


def _noop(_: str) -> None:
    pass


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
        "source": str(value("source", "overpass")),
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
    """Haalt bedrijven op, een branche per keer.

    Een enkele Overpass-query duurt zomaar tien tot dertig seconden, en met een
    handvol branches loopt dat ver over wat een serverless functie mag draaien.
    Daarom wordt na elke branche opgeslagen wat er nog open staat: de volgende
    aanroep pakt de rest op in plaats van weer vooraan te beginnen.
    """
    openstaand = json.loads(database.get_meta(store, "discover_pending") or "[]")
    if not openstaand:
        if not (force or _should_discover(store, settings["discover_every_days"])):
            return 0
        openstaand = [niche.name for niche in campaign.niches]

    gevonden = 0
    branches = 0
    while openstaand and budget.allows(35, branches):
        naam = openstaand[0]
        report(f"Bedrijven ophalen: {naam}...")
        # Nooit langer wachten dan er nog tijd is: anders kapt het platform de
        # functie af terwijl wij nog netjes hadden kunnen opslaan.
        wachttijd = 30.0 if budget.left == float("inf") else max(8.0, budget.left - 8)
        binnen = list(discover(
            campaign, source=settings["source"], only_niche=naam, timeout=wachttijd
        ))
        gevonden += database.upsert_many(store, binnen)
        openstaand.pop(0)
        branches += 1
        database.set_meta(store, "discover_pending", json.dumps(openstaand))
        store.commit()

    if openstaand:
        report(f"{gevonden} nieuw; nog {len(openstaand)} branches te gaan, volgende beurt verder.")
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

    def beoordeel(lead: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        client = None if offline else _client_van_deze_draad(campaign)
        return lead, audit_lead(lead, client=client, offline=offline)

    groep = 1 if offline else AUDIT_WORKERS
    with ThreadPoolExecutor(max_workers=groep) as pool:
        for start in range(0, len(todo), groep):
            if not budget.allows(18, gedaan):
                report("Tijd op; de volgende beurt gaat verder met beoordelen.")
                break
            for lead, resultaat in pool.map(beoordeel, todo[start : start + groep]):
                resultaat["segment"] = campaign.segment(resultaat["score"])
                database.save_audit(store, lead["id"], resultaat)
                gedaan += 1
            store.commit()

    if gedaan:
        report(f"{gedaan} websites beoordeeld.")
    return gedaan


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

    counters = {"discovered": 0, "audited": 0, "demos": 0, "queued": 0, "sent": 0, "failed": 0}
    database.close_stale_runs(store)
    run_id = database.start_run(store, trigger)

    try:
        load_suppression_file(store, campaign)

        # 1. Nieuwe bedrijven ophalen, branche voor branche.
        counters["discovered"] = _discover_step(
            campaign, store, settings, budget, report, force_discover
        )

        # 2. Websites beoordelen, zolang er tijd is.
        todo = database.leads_without_audit(store, settings["audits_per_run"])
        if todo:
            report(f"{len(todo)} websites te beoordelen...")
            counters["audited"] = _audit_step(campaign, store, todo, budget, report, offline)

        # 3. Voorbeeldsites bouwen voor de beste leads die er nog geen hebben.
        # Nog geen demo, of een demo van voor de laatste ontwerpwijziging.
        candidates = [
            row for row in database.ranked_leads(store, limit=settings["demos_per_run"] * 4)
            if (row["score"] or 0) >= settings["min_score"]
            and (not row.get("demo_slug") or row.get("demo_versie") != DEMO_VERSIE)
        ][: settings["demos_per_run"]]
        for row in candidates:
            if not budget.allows(12, counters["demos"]):
                break
            slug, html = build_demo(row, campaign)
            path = None
            if _writes_to_disk():
                from .demo import render_demo

                path = str(render_demo(row, campaign))
            database.record_demo(
                store, row["id"], slug, path=path, url=demo_url_for(slug),
                html=html, versie=DEMO_VERSIE,
            )
            counters["demos"] += 1
        if counters["demos"]:
            store.commit()
            report(f"{counters['demos']} voorbeeldsites gebouwd.")

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
            report(
                f"{counters['queued']} mails klaargezet"
                + (f" (gaan over {wait_hours} uur de deur uit)." if wait_hours else ".")
            )

        # 5. Versturen wat aan de beurt is.
        counters["sent"], counters["failed"] = send_due(
            campaign, store, live=live, report=report, budget=budget
        )

        database.finish_run(store, run_id, counters)
        return counters

    except Exception as exc:  # noqa: BLE001 - een mislukte run mag de autopilot niet slopen
        database.finish_run(store, run_id, counters, status="mislukt", error=str(exc))
        report(f"Fout tijdens de cyclus: {exc}")
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
