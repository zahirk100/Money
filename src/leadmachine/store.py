"""Opslag die zowel op SQLite als op Postgres (Supabase) draait.

Lokaal wil je een bestand dat je kunt kopieren; live wil je een database die
meerdere serverless functies tegelijk aankan. De rest van de code hoeft het
verschil niet te kennen: die praat tegen een Store en schrijft SQL met ?
als plaatshouder.

Twee regels houden de SQL draagbaar:
1. Nooit datumfuncties van de database gebruiken. Tijdstippen komen uit Python
   en gaan als parameter mee. Dat scheelt het hele mijnenveld van datetime('now')
   tegenover now() en van tekstvergelijking tegenover timestamptz.
2. Nieuwe rijen altijd met RETURNING id, niet met lastrowid.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SQLITE_SCHEMA = """
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
    first_seen    TEXT,
    raw           TEXT,
    UNIQUE (osm_type, osm_id)
);
CREATE TABLE IF NOT EXISTS audits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id        INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    checked_at     TEXT,
    reachable      INTEGER, final_url TEXT, status_code INTEGER, https INTEGER,
    mobile_ready   INTEGER, title TEXT, description TEXT, load_ms INTEGER,
    html_bytes     INTEGER, has_contact INTEGER, copyright_year INTEGER,
    platform       TEXT, score INTEGER, segment TEXT, findings TEXT,
    UNIQUE (lead_id)
);
CREATE TABLE IF NOT EXISTS demos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    slug       TEXT NOT NULL,
    path       TEXT,
    url        TEXT,
    html       TEXT,
    created_at TEXT,
    UNIQUE (lead_id)
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL, template TEXT, to_addr TEXT, subject TEXT, body TEXT,
    status     TEXT NOT NULL, error TEXT, send_after TEXT, sent_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT, finished_at TEXT, trigger TEXT, status TEXT,
    discovered  INTEGER DEFAULT 0, audited INTEGER DEFAULT 0, demos INTEGER DEFAULT 0,
    queued      INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS suppression (value TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id            BIGSERIAL PRIMARY KEY,
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
    lat           DOUBLE PRECISION,
    lon           DOUBLE PRECISION,
    source        TEXT,
    first_seen    TIMESTAMPTZ,
    raw           TEXT,
    UNIQUE (osm_type, osm_id)
);
CREATE TABLE IF NOT EXISTS audits (
    id             BIGSERIAL PRIMARY KEY,
    lead_id        BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    checked_at     TIMESTAMPTZ,
    reachable      INTEGER, final_url TEXT, status_code INTEGER, https INTEGER,
    mobile_ready   INTEGER, title TEXT, description TEXT, load_ms INTEGER,
    html_bytes     BIGINT, has_contact INTEGER, copyright_year INTEGER,
    platform       TEXT, score INTEGER, segment TEXT, findings TEXT,
    UNIQUE (lead_id)
);
CREATE TABLE IF NOT EXISTS demos (
    id         BIGSERIAL PRIMARY KEY,
    lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    slug       TEXT NOT NULL,
    path       TEXT,
    url        TEXT,
    html       TEXT,
    created_at TIMESTAMPTZ,
    UNIQUE (lead_id)
);
CREATE TABLE IF NOT EXISTS outreach_log (
    id         BIGSERIAL PRIMARY KEY,
    lead_id    BIGINT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL, template TEXT, to_addr TEXT, subject TEXT, body TEXT,
    status     TEXT NOT NULL, error TEXT, send_after TIMESTAMPTZ, sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ, finished_at TIMESTAMPTZ, trigger TEXT, status TEXT,
    discovered  INTEGER DEFAULT 0, audited INTEGER DEFAULT 0, demos INTEGER DEFAULT 0,
    queued      INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS suppression (value TEXT PRIMARY KEY, reason TEXT, added_at TIMESTAMPTZ);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_audits_score ON audits(score DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach_log(lead_id, created_at);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_log(status, send_after);
CREATE INDEX IF NOT EXISTS idx_demos_slug ON demos(slug);
"""


def now() -> datetime:
    """Alles in UTC. De weergave in het dashboard rekent om naar lokale tijd."""
    return datetime.now(timezone.utc)


def stamp(moment: datetime | None = None) -> str:
    return (moment or now()).strftime("%Y-%m-%d %H:%M:%S")


def is_postgres_url(target: str) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


class Store:
    """Dunne laag boven sqlite3 of psycopg met dezelfde methodes."""

    def __init__(self, target: str | Path) -> None:
        self.target = str(target)
        self.dialect = "postgres" if is_postgres_url(self.target) else "sqlite"
        if self.dialect == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            # Supabase zet een pooler voor de database. In transaction mode
            # (poort 6543) overleeft een prepared statement de volgende query
            # niet, want je krijgt dan telkens een andere sessie. psycopg gaat
            # na een paar herhalingen vanzelf voorbereiden, en dat loopt daar
            # stuk. Uitzetten kost hier vrijwel niets en maakt beide poorten
            # bruikbaar.
            self._conn = psycopg.connect(
                self.target, row_factory=dict_row, autocommit=False, prepare_threshold=None
            )
        else:
            path = Path(self.target)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")

    # -- SQL ----------------------------------------------------------------
    def _translate(self, sql: str) -> str:
        if self.dialect == "sqlite":
            return sql
        # psycopg gebruikt %s; letterlijke procenttekens moeten dan verdubbeld.
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        cur = self._conn.cursor()
        cur.execute(self._translate(sql), tuple(params))
        rows = cur.fetchall() if cur.description else []
        cur.close()
        return [dict(row) for row in rows] if self.dialect == "sqlite" else list(rows)

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Voegt in en geeft het nieuwe id terug. RETURNING werkt in beide."""
        row = self.one(sql.rstrip().rstrip(";") + " RETURNING id", params)
        return int(row["id"]) if row else 0

    def executescript(self, script: str) -> None:
        for statement in filter(None, (s.strip() for s in script.split(";"))):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


class OpslagOntbreekt(RuntimeError):
    """Live draaien zonder database. Terugvallen op een bestand kan daar niet:
    de schijf van een serverless functie is alleen-lezen."""


def is_hosted() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("LM_HOSTED"))


def resolve_target(explicit: str | Path | None = None) -> str:
    """Waar de gegevens staan: expliciet, anders DATABASE_URL, anders lokaal bestand."""
    if explicit:
        return str(explicit)
    for key in ("DATABASE_URL", "POSTGRES_URL", "SUPABASE_DB_URL"):
        value = os.environ.get(key)
        if value:
            return value
    if is_hosted():
        raise OpslagOntbreekt(
            "DATABASE_URL is niet ingesteld. Zonder database kan dit niet draaien: "
            "de schijf van een serverless functie is alleen-lezen. Zet de connection "
            "string van je Supabase-project (Session pooler, poort 5432) in de "
            "omgevingsvariabelen en rol opnieuw uit."
        )
    from .config import DEFAULT_DB

    return str(DEFAULT_DB)


def open_store(target: str | Path | None = None, migrate: bool = True) -> Store:
    store = Store(resolve_target(target))
    if migrate:
        store.executescript(SQLITE_SCHEMA if store.dialect == "sqlite" else POSTGRES_SCHEMA)
        store.executescript(INDEXES)
        _migrate(store)
        store.commit()
    return store


LATER_COLUMNS = {
    "outreach_log": {"send_after": "TEXT", "sent_at": "TEXT"},
    "demos": {"slug": "TEXT", "html": "TEXT"},
}
PG_TYPES = {"send_after": "TIMESTAMPTZ", "sent_at": "TIMESTAMPTZ", "slug": "TEXT", "html": "TEXT"}


def _columns(store: Store, table: str) -> set[str]:
    if store.dialect == "sqlite":
        return {row["name"] for row in store.execute(f"PRAGMA table_info({table})")}
    return {
        row["column_name"]
        for row in store.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
        )
    }


def _migrate(store: Store) -> None:
    """Bestaande databases krijgen later toegevoegde kolommen alsnog."""
    for table, columns in LATER_COLUMNS.items():
        existing = _columns(store, table)
        if not existing:
            continue
        for column, sqlite_type in columns.items():
            if column not in existing:
                ddl = sqlite_type if store.dialect == "sqlite" else PG_TYPES[column]
                store.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
