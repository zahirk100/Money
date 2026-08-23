"""SQLite-opslag. Een bestand, geen server, makkelijk te back-uppen."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DEFAULT_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    osm_type      TEXT NOT NULL,
    osm_id        TEXT NOT NULL,
    name          TEXT NOT NULL,
    niche         TEXT,
    street        TEXT,
    housenumber   TEXT,
    postcode      TEXT,
    city          TEXT,
    phone         TEXT,
    email         TEXT,
    website       TEXT,
    opening_hours TEXT,
    lat           REAL,
    lon           REAL,
    source        TEXT,
    first_seen    TEXT DEFAULT (datetime('now')),
    raw           TEXT,
    UNIQUE (osm_type, osm_id)
);

CREATE TABLE IF NOT EXISTS audits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id        INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    checked_at     TEXT DEFAULT (datetime('now')),
    reachable      INTEGER,
    final_url      TEXT,
    status_code    INTEGER,
    https          INTEGER,
    mobile_ready   INTEGER,
    title          TEXT,
    description    TEXT,
    load_ms        INTEGER,
    html_bytes     INTEGER,
    has_contact    INTEGER,
    copyright_year INTEGER,
    platform       TEXT,
    score          INTEGER,
    segment        TEXT,
    findings       TEXT,
    UNIQUE (lead_id)
);

CREATE TABLE IF NOT EXISTS demos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    url         TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (lead_id)
);

CREATE TABLE IF NOT EXISTS outreach_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    template   TEXT,
    to_addr    TEXT,
    subject    TEXT,
    body       TEXT,
    status     TEXT NOT NULL,
    error      TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suppression (
    value      TEXT PRIMARY KEY,
    reason     TEXT,
    added_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audits_score ON audits(score DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach_log(lead_id, created_at);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else DEFAULT_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_lead(conn: sqlite3.Connection, lead: dict[str, Any]) -> tuple[int, bool]:
    """Voegt een lead toe of werkt hem bij. Geeft (id, is_new) terug."""
    cur = conn.execute(
        "SELECT id FROM leads WHERE osm_type = ? AND osm_id = ?",
        (lead["osm_type"], lead["osm_id"]),
    )
    row = cur.fetchone()
    fields = (
        "name", "niche", "street", "housenumber", "postcode", "city",
        "phone", "email", "website", "opening_hours", "lat", "lon", "source",
    )
    values = [lead.get(f) for f in fields]
    if row:
        conn.execute(
            f"UPDATE leads SET {', '.join(f + ' = ?' for f in fields)}, raw = ? WHERE id = ?",
            [*values, json.dumps(lead.get("raw", {}), ensure_ascii=False), row["id"]],
        )
        return int(row["id"]), False

    cur = conn.execute(
        f"INSERT INTO leads (osm_type, osm_id, {', '.join(fields)}, raw) "
        f"VALUES (?, ?, {', '.join('?' for _ in fields)}, ?)",
        [
            lead["osm_type"],
            lead["osm_id"],
            *values,
            json.dumps(lead.get("raw", {}), ensure_ascii=False),
        ],
    )
    return int(cur.lastrowid), True


def save_audit(conn: sqlite3.Connection, lead_id: int, audit: dict[str, Any]) -> None:
    columns = (
        "reachable", "final_url", "status_code", "https", "mobile_ready",
        "title", "description", "load_ms", "html_bytes", "has_contact",
        "copyright_year", "platform", "score", "segment",
    )
    conn.execute(
        f"INSERT INTO audits (lead_id, {', '.join(columns)}, findings, checked_at) "
        f"VALUES (?, {', '.join('?' for _ in columns)}, ?, datetime('now')) "
        f"ON CONFLICT(lead_id) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in columns)
        + ", findings = excluded.findings, checked_at = excluded.checked_at",
        [
            lead_id,
            *[audit.get(c) for c in columns],
            json.dumps(audit.get("findings", []), ensure_ascii=False),
        ],
    )


def leads_without_audit(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT l.* FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
        "WHERE a.id IS NULL ORDER BY l.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def ranked_leads(
    conn: sqlite3.Connection,
    limit: int | None = None,
    segment: str | None = None,
    niche: str | None = None,
    with_email: bool = False,
    with_phone: bool = False,
) -> list[sqlite3.Row]:
    sql = [
        "SELECT l.*, a.score, a.segment, a.findings, a.final_url, a.reachable,",
        "       d.path AS demo_path, d.url AS demo_url,",
        "       (SELECT COUNT(*) FROM outreach_log o",
        "         WHERE o.lead_id = l.id AND o.status = 'sent') AS sent_count",
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
        sql.append("AND l.email IS NOT NULL AND l.email != ''")
    if with_phone:
        sql.append("AND l.phone IS NOT NULL AND l.phone != ''")
    sql.append("ORDER BY a.score DESC, l.name ASC")
    if limit:
        sql.append(f"LIMIT {int(limit)}")
    return conn.execute("\n".join(sql), params).fetchall()


def record_demo(conn: sqlite3.Connection, lead_id: int, path: str, url: str | None) -> None:
    conn.execute(
        "INSERT INTO demos (lead_id, path, url) VALUES (?, ?, ?) "
        "ON CONFLICT(lead_id) DO UPDATE SET path = excluded.path, url = excluded.url, "
        "created_at = datetime('now')",
        (lead_id, path, url),
    )


def log_outreach(conn: sqlite3.Connection, lead_id: int, **kwargs: Any) -> None:
    conn.execute(
        "INSERT INTO outreach_log (lead_id, channel, template, to_addr, subject, body, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            lead_id,
            kwargs.get("channel", "email"),
            kwargs.get("template"),
            kwargs.get("to_addr"),
            kwargs.get("subject"),
            kwargs.get("body"),
            kwargs.get("status", "drafted"),
            kwargs.get("error"),
        ),
    )


def last_contact(conn: sqlite3.Connection, lead_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(created_at) AS last FROM outreach_log "
        "WHERE lead_id = ? AND status = 'sent'",
        (lead_id,),
    ).fetchone()
    return row["last"] if row else None


def sent_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM outreach_log "
        "WHERE status = 'sent' AND date(created_at) = date('now')"
    ).fetchone()
    return int(row["n"])


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    email = (email or "").lower().strip()
    if not email:
        return False
    domain = email.partition("@")[2]
    row = conn.execute(
        "SELECT 1 FROM suppression WHERE value = ? OR value = ?", (email, domain)
    ).fetchone()
    return row is not None


def suppress(conn: sqlite3.Connection, value: str, reason: str = "handmatig") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO suppression (value, reason) VALUES (?, ?)",
        (value.lower().strip(), reason),
    )
