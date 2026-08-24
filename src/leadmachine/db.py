"""Alle databasebewerkingen, in draagbare SQL.

Werkt op SQLite (lokaal bestand) en op Postgres (Supabase) zonder dat de rest
van de code weet welke van de twee eronder zit. Zie store.py voor de regels
die dat mogelijk maken: geen datumfuncties van de database, en nieuwe rijen
altijd met RETURNING id.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .store import Store, is_postgres_url, now, open_store, stamp  # noqa: F401


def connect(target: str | Path | None = None) -> Store:
    return open_store(target)


@contextmanager
def session(target: str | Path | None = None) -> Iterator[Store]:
    store = open_store(target)
    try:
        yield store
        store.commit()
    finally:
        store.close()


def _as_datetime(value: Any) -> datetime | None:
    """Postgres geeft een datetime terug, SQLite een string."""
    if value is None or isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# -- leads -----------------------------------------------------------------
LEAD_FIELDS = (
    "name", "niche", "street", "housenumber", "postcode", "city",
    "phone", "email", "website", "opening_hours", "lat", "lon", "source",
)


def upsert_lead(store: Store, lead: dict[str, Any]) -> tuple[int, bool]:
    """Voegt toe of werkt bij. Geeft (id, is_nieuw) terug."""
    existing = store.one(
        "SELECT id FROM leads WHERE osm_type = ? AND osm_id = ?",
        (lead["osm_type"], lead["osm_id"]),
    )
    values = [lead.get(field) for field in LEAD_FIELDS]
    raw = json.dumps(lead.get("raw", {}), ensure_ascii=False)

    if existing:
        store.execute(
            f"UPDATE leads SET {', '.join(f + ' = ?' for f in LEAD_FIELDS)}, raw = ? WHERE id = ?",
            [*values, raw, existing["id"]],
        )
        return int(existing["id"]), False

    lead_id = store.insert(
        f"INSERT INTO leads (osm_type, osm_id, {', '.join(LEAD_FIELDS)}, raw, first_seen) "
        f"VALUES (?, ?, {', '.join('?' for _ in LEAD_FIELDS)}, ?, ?)",
        [lead["osm_type"], lead["osm_id"], *values, raw, stamp()],
    )
    return lead_id, True


def upsert_many(store: Store, leads: list[dict[str, Any]], batch: int = 100) -> int:
    """Voegt een reeks bedrijven in een paar opdrachten toe of werkt ze bij.

    Een voor een is elk bedrijf twee keer heen en weer naar de database. Bij een
    paar honderd resultaten kost dat meer tijd dan een serverless functie mag
    draaien. Zo is het twee opdrachten per honderd. Geeft terug hoeveel er nieuw
    waren.
    """
    if not leads:
        return 0

    nieuw = 0
    moment = stamp()
    for start in range(0, len(leads), batch):
        deel = leads[start : start + batch]
        sleutels = [(lead["osm_type"], lead["osm_id"]) for lead in deel]

        voorwaarde = " OR ".join(["(osm_type = ? AND osm_id = ?)"] * len(deel))
        params = [waarde for sleutel in sleutels for waarde in sleutel]
        bestaand = {
            (rij["osm_type"], rij["osm_id"])
            for rij in store.execute(
                f"SELECT osm_type, osm_id FROM leads WHERE {voorwaarde}", params
            )
        }
        nieuw += sum(1 for sleutel in sleutels if sleutel not in bestaand)

        kolommen = ("osm_type", "osm_id", *LEAD_FIELDS, "raw", "first_seen")
        rij_sjabloon = "(" + ", ".join("?" for _ in kolommen) + ")"
        waarden: list[Any] = []
        for lead in deel:
            waarden.extend([
                lead["osm_type"], lead["osm_id"],
                *[lead.get(veld) for veld in LEAD_FIELDS],
                json.dumps(lead.get("raw", {}), ensure_ascii=False),
                moment,
            ])
        store.execute(
            f"INSERT INTO leads ({', '.join(kolommen)}) "
            f"VALUES {', '.join(rij_sjabloon for _ in deel)} "
            "ON CONFLICT (osm_type, osm_id) DO UPDATE SET "
            # first_seen blijft staan: dat is wanneer we het bedrijf voor het
            # eerst zagen, niet wanneer we het voor het laatst bijwerkten.
            + ", ".join(f"{veld} = excluded.{veld}" for veld in (*LEAD_FIELDS, "raw")),
            waarden,
        )
    return nieuw


AUDIT_COLUMNS = (
    "reachable", "final_url", "status_code", "https", "mobile_ready",
    "title", "description", "load_ms", "html_bytes", "has_contact",
    "copyright_year", "platform", "score", "segment",
)


def save_audit(store: Store, lead_id: int, audit: dict[str, Any]) -> None:
    store.execute(
        f"INSERT INTO audits (lead_id, {', '.join(AUDIT_COLUMNS)}, findings, checked_at) "
        f"VALUES (?, {', '.join('?' for _ in AUDIT_COLUMNS)}, ?, ?) "
        "ON CONFLICT (lead_id) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in AUDIT_COLUMNS)
        + ", findings = excluded.findings, checked_at = excluded.checked_at",
        [
            lead_id,
            *[audit.get(c) for c in AUDIT_COLUMNS],
            json.dumps(audit.get("findings", []), ensure_ascii=False),
            stamp(),
        ],
    )


def leads_without_audit(store: Store, limit: int | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT l.* FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
        "WHERE a.id IS NULL ORDER BY l.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return store.execute(sql)


def ranked_leads(
    store: Store,
    limit: int | None = None,
    segment: str | None = None,
    niche: str | None = None,
    with_email: bool = False,
    with_phone: bool = False,
) -> list[dict[str, Any]]:
    sql = [
        "SELECT l.*, a.score, a.segment, a.findings, a.final_url, a.reachable,",
        "       d.path AS demo_path, d.url AS demo_url, d.slug AS demo_slug,",
        "       (SELECT COUNT(*) FROM outreach_log o",
        "         WHERE o.lead_id = l.id AND o.status = 'verstuurd') AS sent_count",
        "FROM leads l",
        "JOIN audits a ON a.lead_id = l.id",
        "LEFT JOIN demos d ON d.lead_id = l.id",
        "WHERE 1 = 1",
    ]
    params: list[Any] = []
    if segment:
        sql.append("AND a.segment = ?")
        params.append(segment)
    if niche:
        sql.append("AND l.niche = ?")
        params.append(niche)
    if with_email:
        sql.append("AND l.email IS NOT NULL AND l.email <> ''")
    if with_phone:
        sql.append("AND l.phone IS NOT NULL AND l.phone <> ''")
    sql.append("ORDER BY a.score DESC, l.name ASC")
    if limit:
        sql.append(f"LIMIT {int(limit)}")
    return store.execute("\n".join(sql), params)


# -- demo's ----------------------------------------------------------------
def record_demo(
    store: Store, lead_id: int, slug: str,
    path: str | None = None, url: str | None = None, html: str | None = None,
) -> None:
    """De pagina gaat ook als HTML de database in, zodat een live omgeving
    zonder schijf hem alsnog kan serveren."""
    store.execute(
        "INSERT INTO demos (lead_id, slug, path, url, html, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (lead_id) DO UPDATE SET slug = excluded.slug, path = excluded.path, "
        "url = excluded.url, html = excluded.html, created_at = excluded.created_at",
        (lead_id, slug, path, url, html, stamp()),
    )


def demo_by_slug(store: Store, slug: str) -> dict[str, Any] | None:
    return store.one("SELECT * FROM demos WHERE slug = ?", (slug,))


# -- outreach --------------------------------------------------------------
def log_outreach(store: Store, lead_id: int, **kwargs: Any) -> int:
    status = kwargs.get("status", "concept")
    return store.insert(
        "INSERT INTO outreach_log (lead_id, channel, template, to_addr, subject, body, "
        "status, error, send_after, sent_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lead_id,
            kwargs.get("channel", "email"),
            kwargs.get("template"),
            kwargs.get("to_addr"),
            kwargs.get("subject"),
            kwargs.get("body"),
            status,
            kwargs.get("error"),
            kwargs.get("send_after"),
            kwargs.get("sent_at") or (stamp() if status == "verstuurd" else None),
            stamp(),
        ),
    )


def queue_outreach(
    store: Store, lead_id: int, message: dict[str, str], template: str, send_after: str
) -> int:
    return log_outreach(
        store, lead_id, channel="email", template=template, to_addr=message["to"],
        subject=message["subject"], body=message["body"], status="wacht", send_after=send_after,
    )


def due_outreach(store: Store, limit: int) -> list[dict[str, Any]]:
    """Wachtrij-items waarvan de wachttijd voorbij is."""
    return store.execute(
        "SELECT o.*, l.name AS lead_name FROM outreach_log o "
        "JOIN leads l ON l.id = o.lead_id "
        "WHERE o.status = 'wacht' AND (o.send_after IS NULL OR o.send_after <= ?) "
        f"ORDER BY o.id LIMIT {int(limit)}",
        (stamp(),),
    )


def pending_outreach(store: Store, limit: int = 100) -> list[dict[str, Any]]:
    return store.execute(
        "SELECT o.*, l.name AS lead_name FROM outreach_log o "
        "JOIN leads l ON l.id = o.lead_id "
        f"WHERE o.status = 'wacht' ORDER BY o.send_after, o.id LIMIT {int(limit)}"
    )


def mark_outreach(store: Store, outreach_id: int, status: str, error: str | None = None) -> None:
    if status == "verstuurd":
        store.execute(
            "UPDATE outreach_log SET status = ?, error = ?, sent_at = ? WHERE id = ?",
            (status, error, stamp(), outreach_id),
        )
    else:
        store.execute(
            "UPDATE outreach_log SET status = ?, error = ? WHERE id = ?",
            (status, error, outreach_id),
        )


def already_queued(store: Store, lead_id: int) -> bool:
    return store.one(
        "SELECT 1 AS x FROM outreach_log WHERE lead_id = ? AND status IN ('wacht', 'verstuurd')",
        (lead_id,),
    ) is not None


def last_contact(store: Store, lead_id: int) -> datetime | None:
    row = store.one(
        "SELECT MAX(COALESCE(sent_at, created_at)) AS laatst FROM outreach_log "
        "WHERE lead_id = ? AND status = 'verstuurd'",
        (lead_id,),
    )
    return _as_datetime(row["laatst"]) if row else None


def sent_today(store: Store) -> int:
    """De dagteller loopt van middernacht UTC tot middernacht UTC."""
    midnight = now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        store.scalar(
            "SELECT COUNT(*) AS n FROM outreach_log "
            "WHERE status = 'verstuurd' AND COALESCE(sent_at, created_at) >= ?",
            (stamp(midnight),),
        )
        or 0
    )


def sent_since(store: Store, days: int) -> list[dict[str, Any]]:
    since = now() - timedelta(days=days)
    return store.execute(
        "SELECT COALESCE(sent_at, created_at) AS moment FROM outreach_log "
        "WHERE status = 'verstuurd' AND COALESCE(sent_at, created_at) >= ?",
        (stamp(since),),
    )


# -- afmeldingen -----------------------------------------------------------
def is_suppressed(store: Store, email: str) -> bool:
    email = (email or "").lower().strip()
    if not email:
        return False
    domain = email.partition("@")[2]
    return store.one(
        "SELECT 1 AS x FROM suppression WHERE value = ? OR value = ?", (email, domain)
    ) is not None


def suppress(store: Store, value: str, reason: str = "handmatig") -> None:
    store.execute(
        "INSERT INTO suppression (value, reason, added_at) VALUES (?, ?, ?) "
        "ON CONFLICT (value) DO UPDATE SET reason = excluded.reason, added_at = excluded.added_at",
        (value.lower().strip(), reason, stamp()),
    )


# -- draaibeurten ----------------------------------------------------------
def start_run(store: Store, trigger: str) -> int:
    run_id = store.insert(
        "INSERT INTO runs (trigger, status, started_at) VALUES (?, 'bezig', ?)",
        (trigger, stamp()),
    )
    store.commit()
    return run_id


def finish_run(
    store: Store, run_id: int, counters: dict[str, int],
    status: str = "klaar", error: str | None = None,
) -> None:
    store.execute(
        "UPDATE runs SET finished_at = ?, status = ?, error = ?, discovered = ?, audited = ?, "
        "demos = ?, queued = ?, sent = ?, failed = ? WHERE id = ?",
        (
            stamp(), status, error,
            counters.get("discovered", 0), counters.get("audited", 0),
            counters.get("demos", 0), counters.get("queued", 0),
            counters.get("sent", 0), counters.get("failed", 0), run_id,
        ),
    )
    store.commit()


def close_stale_runs(store: Store, minuten: int = 15) -> None:
    """Een draaibeurt die door een time-out is afgekapt kan zichzelf niet meer
    afsluiten en zou anders voor altijd op 'bezig' blijven staan."""
    grens = stamp(now() - timedelta(minutes=minuten))
    store.execute(
        "UPDATE runs SET status = 'afgebroken', finished_at = ?, "
        "error = 'afgekapt door een time-out' WHERE status = 'bezig' AND started_at < ?",
        (stamp(), grens),
    )


def recent_runs(store: Store, limit: int = 15) -> list[dict[str, Any]]:
    return store.execute(f"SELECT * FROM runs ORDER BY id DESC LIMIT {int(limit)}")


# -- losse waarden ---------------------------------------------------------
def get_meta(store: Store, key: str) -> str | None:
    row = store.one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else None


def set_meta(store: Store, key: str, value: str) -> None:
    store.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
