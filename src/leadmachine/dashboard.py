"""Lokaal dashboard: zien wat er is gevonden, gebouwd, klaargezet en verstuurd.

Draait op de standaardbibliotheek, geen extra pakketten. Luistert standaard
alleen op 127.0.0.1. Schrijfacties vereisen een token dat bij het starten wordt
gegenereerd en in de pagina wordt gezet, zodat een willekeurige website die je
open hebt staan niet ongemerkt jouw lokale server kan aansturen.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import sqlite3
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import db as database
from . import report as reporting
from .audit import top_pitches
from .config import OUT_DIR, Campaign
from .demo import TEMPLATE_DIR, render_demo
from .outreach import draft_email, eligible
from .pipeline import _stamp, autopilot_settings, run_cycle, send_due

STATE: dict[str, Any] = {"running": False, "log": [], "started": None}
STATE_LOCK = threading.Lock()


def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _overview(conn: sqlite3.Connection, campaign: Campaign) -> dict[str, Any]:
    stats = reporting.stats(conn)

    sent_per_day = _rows(
        conn.execute(
            "SELECT date(COALESCE(sent_at, created_at)) AS dag, COUNT(*) AS aantal "
            "FROM outreach_log WHERE status = 'verstuurd' "
            "AND date(COALESCE(sent_at, created_at)) >= date('now', '-13 days') "
            "GROUP BY dag ORDER BY dag"
        ).fetchall()
    )
    # De grafiek toont altijd veertien dagen, ook de dagen zonder verzendingen.
    by_day = {row["dag"]: row["aantal"] for row in sent_per_day}
    days = _rows(
        conn.execute(
            "WITH RECURSIVE d(dag) AS ("
            "  SELECT date('now', '-13 days') UNION ALL"
            "  SELECT date(dag, '+1 day') FROM d WHERE dag < date('now')"
            ") SELECT dag FROM d"
        ).fetchall()
    )
    series = [{"dag": row["dag"], "aantal": by_day.get(row["dag"], 0)} for row in days]

    # Een trechter telt bedrijven, geen mails: anders kan een latere stap groter
    # zijn dan een eerdere zodra iemand twee keer is gemaild.
    def bedrijven(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0] or 0)

    funnel = [
        {"stap": "Gevonden", "aantal": stats["leads"]},
        {"stap": "Beoordeeld", "aantal": stats["audited"]},
        {"stap": "Demo gebouwd", "aantal": bedrijven("SELECT COUNT(DISTINCT lead_id) FROM demos")},
        {"stap": "Mail klaargezet", "aantal": bedrijven(
            "SELECT COUNT(DISTINCT lead_id) FROM outreach_log "
            "WHERE status IN ('wacht', 'verstuurd', 'mislukt')")},
        {"stap": "Verstuurd", "aantal": bedrijven(
            "SELECT COUNT(DISTINCT lead_id) FROM outreach_log WHERE status = 'verstuurd'")},
    ]

    settings = autopilot_settings(campaign)
    return {
        "stats": stats,
        "sent_per_day": series,
        "funnel": funnel,
        "runs": _rows(database.recent_runs(conn, 10)),
        "queue": _rows(database.pending_outreach(conn, 50)),
        "autopilot": {
            **settings,
            "daily_limit": int(campaign.outreach.get("daily_limit", 25)),
            "area": campaign.area,
        },
        "running": STATE["running"],
    }


def _leads(conn: sqlite3.Connection, query: dict[str, list[str]]) -> list[dict[str, Any]]:
    segment = (query.get("segment") or [""])[0] or None
    niche = (query.get("niche") or [""])[0] or None
    search = (query.get("q") or [""])[0].strip().lower()
    limit = int((query.get("limit") or ["200"])[0])

    result = []
    for row in database.ranked_leads(conn, limit=limit * 2, segment=segment, niche=niche):
        if search and search not in (row["name"] or "").lower():
            continue
        item = dict(row)
        item["pitches"] = top_pitches(json.loads(row["findings"] or "[]"), 3)
        item["heeft_demo"] = bool(row["demo_path"])
        item.pop("raw", None)
        item.pop("findings", None)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _lead_detail(conn: sqlite3.Connection, lead_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT l.*, a.score, a.segment, a.findings, a.final_url, a.checked_at, "
        "       a.load_ms, a.mobile_ready, a.https, a.platform, "
        "       d.path AS demo_path, d.url AS demo_url "
        "FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
        "LEFT JOIN demos d ON d.lead_id = l.id WHERE l.id = ?",
        (lead_id,),
    ).fetchone()
    if not row:
        return None
    detail = dict(row)
    detail["findings"] = json.loads(row["findings"] or "[]")
    detail.pop("raw", None)
    detail["outreach"] = _rows(
        conn.execute(
            "SELECT id, status, subject, body, to_addr, template, send_after, sent_at, "
            "created_at, error FROM outreach_log WHERE lead_id = ? ORDER BY id DESC",
            (lead_id,),
        ).fetchall()
    )
    return detail


def _run_in_background(campaign: Campaign, db_path: str, trigger: str) -> None:
    def worker() -> None:
        conn = database.connect(db_path)
        try:
            run_cycle(campaign, conn, trigger=trigger, report=_log)
        except Exception as exc:  # noqa: BLE001 - fout hoort in het dashboard, niet in een crash
            _log(f"Cyclus afgebroken: {exc}")
        finally:
            conn.close()
            with STATE_LOCK:
                STATE["running"] = False

    with STATE_LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["log"] = []
        STATE["started"] = datetime.now().isoformat(timespec="seconds")
    threading.Thread(target=worker, daemon=True).start()


def _log(message: str) -> None:
    with STATE_LOCK:
        STATE["log"].append(f"{datetime.now():%H:%M:%S}  {message}")
        del STATE["log"][:-200]


def make_handler(campaign: Campaign, db_path: str, token: str) -> type[BaseHTTPRequestHandler]:
    page = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "LeadMachine"

        def log_message(self, *args: Any) -> None:  # stil, tenzij er iets misgaat
            pass

        # -- helpers ------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def _conn(self) -> sqlite3.Connection:
            return database.connect(db_path)

        def _authorised(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-LM-Token", ""), token)

        # -- routes -------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

            if path == "/":
                html = page.replace("__TOKEN__", token)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return

            if path.startswith("/demo/"):
                self._serve_demo(path[len("/demo/"):])
                return

            if not path.startswith("/api/"):
                self._json({"fout": "niet gevonden"}, 404)
                return

            conn = self._conn()
            try:
                if path == "/api/overview":
                    self._json(_overview(conn, campaign))
                elif path == "/api/leads":
                    self._json(_leads(conn, query))
                elif path.startswith("/api/lead/"):
                    detail = _lead_detail(conn, int(path.rsplit("/", 1)[1]))
                    self._json(detail or {"fout": "onbekende lead"}, 200 if detail else 404)
                elif path == "/api/outreach":
                    status = (query.get("status") or [""])[0]
                    sql = (
                        "SELECT o.*, l.name AS lead_name FROM outreach_log o "
                        "JOIN leads l ON l.id = o.lead_id "
                        + ("WHERE o.status = ? " if status else "")
                        + "ORDER BY o.id DESC LIMIT 300"
                    )
                    rows = conn.execute(sql, (status,) if status else ()).fetchall()
                    self._json(_rows(rows))
                elif path == "/api/run/status":
                    with STATE_LOCK:
                        self._json({"running": STATE["running"], "log": list(STATE["log"])})
                else:
                    self._json({"fout": "niet gevonden"}, 404)
            except (ValueError, sqlite3.Error) as exc:
                self._json({"fout": str(exc)}, 400)
            finally:
                conn.close()

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                self._json({"fout": "token ontbreekt of klopt niet"}, 403)
                return

            path = urllib.parse.urlparse(self.path).path
            conn = self._conn()
            try:
                if path == "/api/run":
                    _run_in_background(campaign, db_path, "dashboard")
                    self._json({"gestart": True})
                elif path == "/api/send-now":
                    sent, failed = send_due(campaign, conn, live=True, report=_log)
                    conn.commit()
                    self._json({"verstuurd": sent, "mislukt": failed})
                elif path.startswith("/api/lead/"):
                    self._lead_action(conn, path)
                elif path.startswith("/api/outreach/"):
                    self._outreach_action(conn, path)
                else:
                    self._json({"fout": "niet gevonden"}, 404)
            except (ValueError, sqlite3.Error) as exc:
                self._json({"fout": str(exc)}, 400)
            finally:
                conn.close()

        def _lead_action(self, conn: sqlite3.Connection, path: str) -> None:
            _, _, _, lead_id, action = path.split("/", 4)
            row = conn.execute(
                "SELECT l.*, a.score, a.segment, a.findings, d.url AS demo_url, d.path AS demo_path "
                "FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
                "LEFT JOIN demos d ON d.lead_id = l.id WHERE l.id = ?",
                (int(lead_id),),
            ).fetchone()
            if not row:
                self._json({"fout": "onbekende lead"}, 404)
                return

            if action == "demo":
                page_path = render_demo(row, campaign)
                import os as _os

                base = _os.environ.get("DEMO_BASE_URL", "").rstrip("/")
                database.record_demo(
                    conn, row["id"], str(page_path),
                    f"{base}/{page_path.parent.name}/" if base else None,
                )
                conn.commit()
                self._json({"demo": f"/demo/{page_path.parent.name}/"})
            elif action == "queue":
                ok, reason = eligible(conn, row, campaign)
                if not ok:
                    self._json({"fout": reason}, 400)
                    return
                message = draft_email(row, campaign, demo_url=row["demo_url"])
                settings = autopilot_settings(campaign)
                from datetime import timedelta

                wait = 0 if settings["send_mode"] == "auto" else settings["review_hours"]
                send_after = _stamp(datetime.utcnow() + timedelta(hours=wait))
                database.queue_outreach(conn, row["id"], message, "first", send_after)
                conn.commit()
                self._json({"klaargezet": True, "verstuurt_na": send_after})
            elif action == "suppress":
                if row["email"]:
                    database.suppress(conn, row["email"], "via dashboard")
                conn.execute(
                    "UPDATE outreach_log SET status = 'geannuleerd' "
                    "WHERE lead_id = ? AND status = 'wacht'",
                    (row["id"],),
                )
                conn.commit()
                self._json({"afgemeld": True})
            else:
                self._json({"fout": "onbekende actie"}, 404)

        def _outreach_action(self, conn: sqlite3.Connection, path: str) -> None:
            _, _, _, outreach_id, action = path.split("/", 4)
            if action == "cancel":
                database.mark_outreach(conn, int(outreach_id), "geannuleerd", "handmatig")
                conn.commit()
                self._json({"geannuleerd": True})
            elif action == "now":
                conn.execute(
                    "UPDATE outreach_log SET send_after = datetime('now') WHERE id = ? AND status = 'wacht'",
                    (int(outreach_id),),
                )
                conn.commit()
                sent, failed = send_due(campaign, conn, live=True, report=_log, limit=1)
                conn.commit()
                self._json({"verstuurd": sent, "mislukt": failed})
            else:
                self._json({"fout": "onbekende actie"}, 404)

        def _serve_demo(self, relative: str) -> None:
            root = (OUT_DIR / "demos").resolve()
            target = (root / urllib.parse.unquote(relative)).resolve()
            if target.is_dir():
                target = target / "index.html"
            # Nooit buiten de demomap serveren. is_relative_to en niet startswith:
            # anders zou een map met dezelfde naamstam er ook doorheen glippen.
            if not target.is_relative_to(root) or not target.is_file():
                self._send(404, b"Demo niet gevonden", "text/plain; charset=utf-8")
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), content_type)

    return Handler


def serve(campaign: Campaign, db_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    token = secrets.token_urlsafe(24)
    handler = make_handler(campaign, str(db_path), token)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Dashboard draait op http://{host}:{port}")
    if host not in {"127.0.0.1", "localhost"}:
        print("Let op: je stelt het dashboard open buiten deze computer. Dat is geen goed idee.")
    print("Stoppen met Ctrl-C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard gestopt.")
    finally:
        httpd.server_close()
