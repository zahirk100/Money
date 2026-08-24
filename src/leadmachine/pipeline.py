"""De volledige cyclus in een keer: zoeken, beoordelen, bouwen, opstellen, versturen.

Dit is wat de autopilot elke dag draait en wat de knop 'nu draaien' in het
dashboard aanroept. Elke stap houdt zich aan de limieten uit de config, en
elke draaibeurt wordt vastgelegd in de tabel runs, zodat je in het dashboard
kunt terugzien wat er is gebeurd.
"""

from __future__ import annotations

import os
import sqlite3
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db as database
from .audit import audit_lead
from .config import Campaign, load_dotenv
from .demo import render_demo
from .discover import discover
from .http import PoliteClient
from .outreach import Mailer, OutreachError, draft_email, eligible, load_suppression_file

Reporter = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _stamp(moment: datetime) -> str:
    """SQLite vergelijkt datums als tekst, en datetime('now') gebruikt een spatie
    als scheidingsteken. Met de T van isoformat valt de vergelijking verkeerd uit
    en blijft een mail een dag in de wachtrij staan."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def autopilot_settings(campaign: Campaign) -> dict[str, Any]:
    raw = campaign._raw.get("autopilot") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "run_at": str(raw.get("run_at", "09:15")),
        "discover_every_days": int(raw.get("discover_every_days", 7)),
        "audits_per_run": int(raw.get("audits_per_run", 200)),
        "demos_per_run": int(raw.get("demos_per_run", 25)),
        "mails_per_run": int(raw.get("mails_per_run", 20)),
        "send_mode": str(raw.get("send_mode", "review")),
        "review_hours": int(raw.get("review_hours", 12)),
        "min_score": int(raw.get("min_score", 45)),
        "source": str(raw.get("source", "overpass")),
    }


def _should_discover(conn: sqlite3.Connection, every_days: int) -> bool:
    last = database.get_meta(conn, "last_discover")
    if not last:
        return True
    try:
        return datetime.utcnow() - datetime.fromisoformat(last) >= timedelta(days=every_days)
    except ValueError:
        return True


def run_cycle(
    campaign: Campaign,
    conn: sqlite3.Connection,
    trigger: str = "handmatig",
    live: bool | None = None,
    report: Reporter = _noop,
    force_discover: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    """Draait een volledige cyclus. live=None betekent: volg de config."""
    load_dotenv()
    settings = autopilot_settings(campaign)
    if live is None:
        live = settings["enabled"]

    counters = {"discovered": 0, "audited": 0, "demos": 0, "queued": 0, "sent": 0, "failed": 0}
    run_id = database.start_run(conn, trigger)
    base_url = os.environ.get("DEMO_BASE_URL", "").rstrip("/")

    try:
        load_suppression_file(conn, campaign)

        # 1. Nieuwe bedrijven ophalen - niet elke dag, dat levert toch niets nieuws op.
        if force_discover or _should_discover(conn, settings["discover_every_days"]):
            report("Bedrijven ophalen uit OpenStreetMap...")
            for lead in discover(campaign, source=settings["source"]):
                _, is_new = database.upsert_lead(conn, lead)
                counters["discovered"] += int(is_new)
            database.set_meta(conn, "last_discover", _stamp(datetime.utcnow()))
            conn.commit()
            report(f"{counters['discovered']} nieuwe bedrijven gevonden.")
        else:
            report("Ophalen overgeslagen (recent genoeg gedaan).")

        # 2. Websites beoordelen.
        todo = database.leads_without_audit(conn, settings["audits_per_run"])
        if todo:
            report(f"{len(todo)} websites beoordelen...")
            client = None if offline else PoliteClient(
                user_agent=str(campaign.audit.get("user_agent", "LeadMachine/1.0")),
                timeout=float(campaign.audit.get("timeout_seconds", 12)),
                delay=float(campaign.audit.get("delay_seconds", 1.5)),
                respect_robots=bool(campaign.audit.get("respect_robots", True)),
            )
            for lead in todo:
                result = audit_lead(lead, client=client, offline=offline)
                result["segment"] = campaign.segment(result["score"])
                database.save_audit(conn, lead["id"], result)
                counters["audited"] += 1
            conn.commit()

        # 3. Voorbeeldsites bouwen voor de beste leads die er nog geen hebben.
        candidates = [
            row for row in database.ranked_leads(conn, limit=settings["demos_per_run"] * 3)
            if not row["demo_path"] and (row["score"] or 0) >= settings["min_score"]
        ][: settings["demos_per_run"]]
        for row in candidates:
            path = render_demo(row, campaign)
            database.record_demo(
                conn, row["id"], str(path),
                f"{base_url}/{path.parent.name}/" if base_url else None,
            )
            counters["demos"] += 1
        if counters["demos"]:
            conn.commit()
            report(f"{counters['demos']} voorbeeldsites gebouwd.")

        # 4. Mails opstellen en in de wachtrij zetten.
        wait_hours = 0 if settings["send_mode"] == "auto" else settings["review_hours"]
        send_after = _stamp(datetime.utcnow() + timedelta(hours=wait_hours))
        for row in database.ranked_leads(conn, limit=settings["mails_per_run"] * 4, with_email=True):
            if counters["queued"] >= settings["mails_per_run"]:
                break
            if (row["score"] or 0) < settings["min_score"]:
                continue
            if database.already_queued(conn, row["id"]):
                continue
            ok, _reason = eligible(conn, row, campaign)
            if not ok:
                continue
            message = draft_email(row, campaign, demo_url=row["demo_url"])
            database.queue_outreach(conn, row["id"], message, "first", send_after)
            counters["queued"] += 1
        if counters["queued"]:
            conn.commit()
            report(
                f"{counters['queued']} mails klaargezet"
                + (f" (gaan over {wait_hours} uur de deur uit)." if wait_hours else ".")
            )

        # 5. Versturen wat aan de beurt is.
        sent, failed = send_due(campaign, conn, live=live, report=report)
        counters["sent"], counters["failed"] = sent, failed

        database.finish_run(conn, run_id, counters)
        return counters

    except Exception as exc:  # noqa: BLE001 - een mislukte run mag de autopilot niet slopen
        database.finish_run(conn, run_id, counters, status="mislukt", error=str(exc))
        report(f"Fout tijdens de cyclus: {exc}")
        traceback.print_exc()
        raise


def send_due(
    campaign: Campaign,
    conn: sqlite3.Connection,
    live: bool = False,
    report: Reporter = _noop,
    limit: int | None = None,
) -> tuple[int, int]:
    """Verstuurt wachtrij-items waarvan de wachttijd voorbij is."""
    daily_limit = int(campaign.outreach.get("daily_limit", 25))
    room = max(0, daily_limit - database.sent_today(conn))
    budget = min(room, limit) if limit else room
    if budget <= 0:
        report("Dagelijkse limiet bereikt; vandaag niets meer versturen.")
        return 0, 0

    due = database.due_outreach(conn, budget)
    if not due:
        return 0, 0

    if not live:
        report(f"{len(due)} mails staan klaar, maar versturen staat uit (proefdraai).")
        return 0, 0

    from .outreach import throttle

    # Vlak voor verzending nogmaals toetsen: er kan intussen een afmelding
    # binnengekomen zijn. Dit gebeurt voordat de SMTP-verbinding opengaat, zodat
    # een lijst met alleen afmeldingen niet eens een verbinding kost.
    queue = []
    for item in due:
        if database.is_suppressed(conn, item["to_addr"] or ""):
            database.mark_outreach(conn, item["id"], "geannuleerd", "afmeldlijst")
            report(f"overgeslagen: {item['to_addr']} staat op de afmeldlijst")
        else:
            queue.append(item)
    conn.commit()
    if not queue:
        return 0, 0

    sent = failed = 0
    try:
        with Mailer(campaign, live=True) as mailer:
            for index, item in enumerate(queue):
                try:
                    mailer.send(item["to_addr"], item["subject"], item["body"])
                    database.mark_outreach(conn, item["id"], "verstuurd")
                    sent += 1
                    report(f"verstuurd naar {item['to_addr']} ({item['lead_name']})")
                except Exception as exc:  # noqa: BLE001
                    database.mark_outreach(conn, item["id"], "mislukt", str(exc))
                    failed += 1
                    report(f"mislukt naar {item['to_addr']}: {exc}")
                conn.commit()
                if index < len(queue) - 1:
                    throttle(campaign)
    except OutreachError as exc:
        report(str(exc))
        return sent, failed
    return sent, failed
