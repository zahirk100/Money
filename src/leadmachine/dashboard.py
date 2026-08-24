"""Het dashboard: zien wat er is gevonden, gebouwd, klaargezet en verstuurd.

Draait op de standaardbibliotheek, zodat dezelfde code lokaal werkt en als
serverless functie op Vercel. Twee dingen verschillen live:

- er hoort een wachtwoord op, want de pagina staat dan op het open internet;
- de voorbeeldsites komen uit de database in plaats van van schijf, want een
  serverless omgeving heeft geen schijf om ze op te bewaren.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import db as database
from . import report as reporting
from .audit import top_pitches
from .config import OUT_DIR, Campaign
from .demo import DEMO_VERSIE, TEMPLATE_DIR, build_demo, demo_slug
from .outreach import draft_email, eligible
from .pipeline import autopilot_settings, demo_url_for, run_cycle, send_due
from .store import OpslagOntbreekt, Store, now, open_store, stamp

STATE: dict[str, Any] = {"running": False, "log": [], "started": None}
STATE_LOCK = threading.Lock()
SESSION_HOURS = 24 * 14


# -- inloggen --------------------------------------------------------------
def dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def is_hosted() -> bool:
    """Draaien we op een hostingplatform in plaats van op je eigen machine?"""
    return bool(os.environ.get("VERCEL") or os.environ.get("LM_HOSTED"))


# Zonder deze twee kan een live omgeving niet werken. De rest is optioneel:
# je kunt prima eerst rondkijken zonder te kunnen mailen.
VEREIST = [
    ("DATABASE_URL", "de connection string van je Supabase-project "
                     "(Project Settings &rsaquo; Database &rsaquo; Session pooler)"),
    ("DASHBOARD_PASSWORD", "het wachtwoord waarmee jij hier inlogt"),
]


def uitleg_bij_databasefout(exc: Exception) -> str:
    """Maakt van een technische verbindingsfout een zin waar je iets aan hebt."""
    tekst = str(exc)
    laag = tekst.lower()
    url = os.environ.get("DATABASE_URL", "")

    if "your-password" in url.lower():
        return ("In DATABASE_URL staat nog de plaatshouder [YOUR-PASSWORD]. "
                "Vervang die door je databasewachtwoord.")
    if "password authentication failed" in laag or "wachtwoord" in laag:
        return ("De database weigert het wachtwoord uit DATABASE_URL. Controleer het, "
                "of stel het opnieuw in via Project Settings > Database.")
    if "network is unreachable" in laag or "cannot assign requested address" in laag:
        return ("De database is niet bereikbaar. Gebruik je de directe verbinding? "
                "Die werkt alleen over IPv6; neem de Session pooler (poort 5432).")
    if "could not translate host name" in laag or "name or service not known" in laag:
        return "De hostnaam uit DATABASE_URL bestaat niet. Kopieer de string opnieuw."
    if "timeout" in laag or "timed out" in laag:
        return "De database antwoordde niet op tijd. Staat het project misschien gepauzeerd?"
    if "circuitbreaker" in laag or "too many authentication failures" in laag:
        return ("Supabase heeft nieuwe verbindingen tijdelijk geblokkeerd na te veel mislukte "
                "inlogpogingen. Controleer eerst het wachtwoord in DATABASE_URL en wacht dan "
                "een paar minuten; daarna gaat de blokkade vanzelf weer open. Blijf niet "
                "verversen, want elke poging houdt hem in stand.")
    if "too many clients" in laag or "max clients" in laag:
        return ("De database heeft te veel gelijktijdige verbindingen. "
                "Even wachten en opnieuw proberen.")
    if "no module named" in laag and "psycopg" in laag:
        return "Het pakket psycopg ontbreekt in de installatie."
    return f"Verbinden met de database lukt niet: {tekst}"


def _database_check(target: str | None = None) -> tuple[bool, str]:
    try:
        store = open_store(target)
    except OpslagOntbreekt as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, uitleg_bij_databasefout(exc)
    try:
        store.scalar("SELECT 1 AS een")
        return True, f"bereikbaar ({store.dialect})"
    except Exception as exc:  # noqa: BLE001
        return False, uitleg_bij_databasefout(exc)
    finally:
        store.close()


def _ontbrekende_instellingen(target: str | None = None) -> list[tuple[str, str]]:
    """Een expliciet meegegeven database telt als ingevuld; dan is de
    omgevingsvariabele niet nodig."""
    ontbreekt = []
    for naam, uitleg in VEREIST:
        if naam == "DATABASE_URL" and target:
            continue
        if not os.environ.get(naam, "").strip():
            ontbreekt.append((naam, uitleg))
    return ontbreekt


def _secret() -> bytes:
    raw = os.environ.get("SESSION_SECRET") or dashboard_password() or "lokaal"
    return hashlib.sha256(raw.encode()).digest()


def make_session_cookie() -> str:
    expires = int((now() + timedelta(hours=SESSION_HOURS)).timestamp())
    signature = hmac.new(_secret(), str(expires).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{expires}.{signature}"


def valid_session(cookie: str) -> bool:
    expires, _, signature = (cookie or "").partition(".")
    if not expires.isdigit():
        return False
    expected = hmac.new(_secret(), expires.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(signature, expected) and int(expires) > now().timestamp()


SETUP_PAGE = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nog even instellen</title>
<style>
:root{color-scheme:light;--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--border:rgba(11,11,11,.10);--brand:#2a78d6;--warn:#fab219}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--plane:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--border:rgba(255,255,255,.10);--brand:#3987e5}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--plane);color:var(--ink);
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px}
main{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:32px;width:min(94vw,620px)}
h1{font-size:1.25rem;margin:0 0 6px;letter-spacing:-.02em}
p{color:var(--ink2);font-size:.93rem;margin:0 0 18px;line-height:1.6}
ul{margin:0;padding-left:20px}li{margin-bottom:10px;font-size:.93rem}
code{background:var(--plane);border:1px solid var(--border);border-radius:5px;padding:1px 6px;font-size:.86em}
.stap{color:var(--ink2);font-size:.86rem;margin-top:22px;border-top:1px solid var(--border);padding-top:16px}
</style></head><body><main>
<h1>Bijna klaar - er ontbreekt nog wat</h1>
<p>Zet deze omgevingsvariabelen in Vercel onder <strong>Settings &rsaquo; Environment
Variables</strong> en rol daarna opnieuw uit (Deployments &rsaquo; &hellip; &rsaquo; Redeploy).</p>
<ul>__ONTBREEKT__</ul>
<p class="stap">De demopagina&#39;s voor je klanten werken zodra <code>DATABASE_URL</code> staat.
Mailen kan pas als <code>RESEND_API_KEY</code> is ingevuld en je domein bij Resend geverifieerd is.</p>
</main></body></html>"""

LOGIN_PAGE = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lead-machine</title>
<style>
:root{color-scheme:light;--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--border:rgba(11,11,11,.10);--brand:#2a78d6}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--plane:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--border:rgba(255,255,255,.10);--brand:#3987e5}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--plane);color:var(--ink);
font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
form{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:32px;width:min(92vw,360px)}
h1{font-size:1.2rem;margin:0 0 4px;letter-spacing:-.02em}p{color:var(--ink2);font-size:.9rem;margin:0 0 20px}
input{width:100%;padding:11px 13px;border:1px solid var(--border);border-radius:9px;background:var(--plane);
color:var(--ink);font:inherit;margin-bottom:12px}
button{width:100%;padding:11px;border:0;border-radius:9px;background:var(--brand);color:#fff;font:inherit;
font-weight:600;cursor:pointer}
.fout{color:#d03b3b;font-size:.85rem;margin:0 0 12px}
</style></head><body><form method="post" action="/login">
<h1>Lead<span style="color:var(--brand)">machine</span></h1>
<p>Even inloggen om verder te gaan.</p>__FOUT__
<input type="password" name="wachtwoord" placeholder="Wachtwoord" autofocus autocomplete="current-password">
<button type="submit">Inloggen</button></form></body></html>"""


# -- gegevens voor de pagina ----------------------------------------------
def _overview(store: Store, campaign: Campaign) -> dict[str, Any]:
    stats = reporting.stats(store)

    # De reeks van veertien dagen bouwen we in Python: datumfuncties verschillen
    # per database, en dit is een handvol rijen.
    buckets: dict[str, int] = {}
    for row in database.sent_since(store, 14):
        moment = database._as_datetime(row["moment"])
        if moment:
            buckets[moment.strftime("%Y-%m-%d")] = buckets.get(moment.strftime("%Y-%m-%d"), 0) + 1
    today = now().date()
    series = [
        {"dag": (today - timedelta(days=13 - i)).isoformat(),
         "aantal": buckets.get((today - timedelta(days=13 - i)).isoformat(), 0)}
        for i in range(14)
    ]

    # Een trechter telt bedrijven, geen mails: anders kan een latere stap groter
    # zijn dan een eerdere zodra iemand twee keer is gemaild.
    def bedrijven(sql: str) -> int:
        return int(store.scalar(sql) or 0)

    funnel = [
        {"stap": "Gevonden", "aantal": stats["leads"]},
        {"stap": "Beoordeeld", "aantal": stats["audited"]},
        {"stap": "Demo gebouwd", "aantal": bedrijven("SELECT COUNT(DISTINCT lead_id) AS n FROM demos")},
        {"stap": "Mail klaargezet", "aantal": bedrijven(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM outreach_log "
            "WHERE status IN ('wacht', 'verstuurd', 'mislukt')")},
        {"stap": "Verstuurd", "aantal": bedrijven(
            "SELECT COUNT(DISTINCT lead_id) AS n FROM outreach_log WHERE status = 'verstuurd'")},
    ]

    # Alle branches die in de database zitten, niet alleen die met een oordeel:
    # anders mist het filter juist de bedrijven die je nog moet bekijken.
    branches = [
        rij["niche"]
        for rij in store.execute(
            "SELECT niche, COUNT(*) AS n FROM leads WHERE niche IS NOT NULL "
            "GROUP BY niche ORDER BY n DESC"
        )
    ]

    settings = autopilot_settings(campaign)
    return {
        "stats": stats,
        "niches": branches,
        "sent_per_day": series,
        "funnel": funnel,
        "runs": database.recent_runs(store, 10),
        "queue": database.pending_outreach(store, 50),
        "autopilot": {
            **settings,
            "daily_limit": int(campaign.outreach.get("daily_limit", 25)),
            "area": ", ".join(campaign.areas),
            "opslag": store.dialect,
        },
        "running": STATE["running"],
    }


def _leads(store: Store, query: dict[str, list[str]]) -> list[dict[str, Any]]:
    segment = (query.get("segment") or [""])[0] or None
    niche = (query.get("niche") or [""])[0] or None
    search = (query.get("q") or [""])[0].strip().lower()
    limit = min(int((query.get("limit") or ["200"])[0]), 1000)

    result = []
    for row in database.ranked_leads(
        store, limit=limit * 2, segment=segment, niche=niche, include_unaudited=not segment
    ):
        if search and search not in (row["name"] or "").lower():
            continue
        item = dict(row)
        item["pitches"] = top_pitches(json.loads(row["findings"] or "[]"), 3)
        item["heeft_demo"] = bool(row.get("demo_slug"))
        item.pop("raw", None)
        item.pop("findings", None)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _lead_detail(store: Store, lead_id: int) -> dict[str, Any] | None:
    row = store.one(
        "SELECT l.*, a.score, a.segment, a.findings, a.final_url, a.checked_at, "
        "       a.load_ms, a.mobile_ready, a.https, a.platform, "
        "       d.slug AS demo_slug, d.url AS demo_url "
        "FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
        "LEFT JOIN demos d ON d.lead_id = l.id WHERE l.id = ?",
        (lead_id,),
    )
    if not row:
        return None
    detail = dict(row)
    detail["findings"] = json.loads(row["findings"] or "[]")
    detail.pop("raw", None)
    detail["outreach"] = store.execute(
        "SELECT id, status, subject, body, to_addr, template, send_after, sent_at, "
        "created_at, error FROM outreach_log WHERE lead_id = ? ORDER BY id DESC",
        (lead_id,),
    )
    return detail


def _log(message: str) -> None:
    with STATE_LOCK:
        STATE["log"].append(f"{datetime.now():%H:%M:%S}  {message}")
        del STATE["log"][:-200]


def _run_in_background(campaign: Campaign, target: str | None, trigger: str) -> None:
    def worker() -> None:
        store = open_store(target)
        try:
            run_cycle(campaign, store, trigger=trigger, report=_log)
        except Exception as exc:  # noqa: BLE001 - fout hoort in het dashboard, niet in een crash
            _log(f"Cyclus afgebroken: {exc}")
        finally:
            store.close()
            with STATE_LOCK:
                STATE["running"] = False

    with STATE_LOCK:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["log"] = []
        STATE["started"] = stamp()
    threading.Thread(target=worker, daemon=True).start()


# -- de webserver ----------------------------------------------------------
def make_handler(
    campaign: Campaign, target: str | None, token: str
) -> type[BaseHTTPRequestHandler]:
    def dashboard_pagina() -> str:
        bestand = TEMPLATE_DIR / "dashboard.html"
        if not bestand.exists():
            raise FileNotFoundError(
                f"templates/dashboard.html ontbreekt (gezocht in {TEMPLATE_DIR}). "
                "Live betekent dat meestal dat de map templates niet is meegepakt."
            )
        return bestand.read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "LeadMachine"

        def log_message(self, *args: Any) -> None:
            pass

        # -- hulpjes ---------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self._send(status, body, "application/json; charset=utf-8")

        def _store(self) -> Store:
            return open_store(target)

        def _store_of_fout(self) -> Store | None:
            """Geeft de verbinding terug, of stuurt zelf een leesbaar antwoord.

            Zonder dit liep een mislukte verbinding buiten alle afhandeling om
            en kreeg je een kale 500 waar niets uit op te maken viel.
            """
            try:
                return self._store()
            except Exception as exc:  # noqa: BLE001
                self._json(
                    {"fout": uitleg_bij_databasefout(exc), "waar": "database"},
                    503,
                )
                return None

        def _route(self) -> tuple[str, dict[str, list[str]]]:
            """Het pad waar het verzoek eigenlijk voor bedoeld was.

            Vercel stuurt alles naar deze ene functie. Bij de nieuwere
            routering komt het verzoek binnen op het pad van de bestemming
            (/api/index) in plaats van op het oorspronkelijke pad, dus geeft de
            rewrite dat pad mee als __lm_path. Draait de code ergens anders,
            dan staat dat er niet en is het pad gewoon het pad.
            """
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            meegegeven = (query.pop("__lm_path", [""]) or [""])[0]
            path = meegegeven or parsed.path
            if not path.startswith("/"):
                path = "/" + path
            return path, query

        def _cookies(self) -> dict[str, str]:
            raw = self.headers.get("Cookie", "")
            out = {}
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key:
                    out[key] = value
            return out

        def _logged_in(self) -> bool:
            if not dashboard_password():
                # Lokaal is een open dashboard prima. Op het open internet zou
                # het je hele leadbestand en de verzendknop weggeven, dus daar
                # gaat de deur dicht in plaats van open.
                return not is_hosted()
            return valid_session(self._cookies().get("lm_sessie", ""))

        def _authorised(self) -> bool:
            return self._logged_in() and secrets.compare_digest(
                self.headers.get("X-LM-Token", ""), token
            )

        def _cron(self) -> None:
            """Draait een stuk van de cyclus. Vercel roept dit aan volgens het
            schema in vercel.json en stuurt CRON_SECRET mee als Bearer-token.
            Zonder secret is het eindpunt dicht: anders kan iedereen die de URL
            kent jouw campagne laten draaien."""
            secret = os.environ.get("CRON_SECRET", "")
            gegeven = self.headers.get("Authorization", "")
            if not secret or not (
                secrets.compare_digest(gegeven, f"Bearer {secret}")
                or secrets.compare_digest(self.headers.get("X-Cron-Secret", ""), secret)
            ):
                self._json({"fout": "CRON_SECRET ontbreekt of klopt niet"}, 401)
                return

            budget = float(os.environ.get("CRON_BUDGET_SECONDS", "35"))
            try:
                store = self._store()
            except OpslagOntbreekt as exc:
                self._json({"status": "mislukt", "fout": str(exc)}, 503)
                return
            try:
                counters = run_cycle(
                    campaign, store, trigger="cron", budget_seconds=budget, report=_log
                )
                self._json({"status": "klaar", **counters})
            except Exception as exc:  # noqa: BLE001 - liever een leesbare fout dan een kale 500
                self._json({"status": "mislukt", "fout": str(exc)}, 500)
            finally:
                store.close()

        def _setup_page(self) -> None:
            items = "".join(
                f"<li><code>{naam}</code> &mdash; {uitleg}</li>"
                for naam, uitleg in _ontbrekende_instellingen(target)
            )
            html = SETUP_PAGE.replace("__ONTBREEKT__", items)
            self._send(503, html.encode(), "text/html; charset=utf-8")

        def _login_page(self, fout: bool = False) -> None:
            html = LOGIN_PAGE.replace(
                "__FOUT__", '<p class="fout">Dat wachtwoord klopt niet.</p>' if fout else ""
            )
            self._send(401 if fout else 200, html.encode(), "text/html; charset=utf-8")

        # -- routes ----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path, query = self._route()

            if path.startswith("/demo/"):
                self._serve_demo(path[len("/demo/"):])   # openbaar: dit is de pagina die je klant opent
                return
            if path == "/api/cron":
                self._cron()
                return
            if path in {"/gezond", "/health"}:
                ontbreekt = [naam for naam, _ in _ontbrekende_instellingen(target)]
                database_ok, database_melding = _database_check(target)
                self._json({
                    "status": "ok" if database_ok and not ontbreekt else "instellen",
                    "klaar": database_ok and not ontbreekt,
                    "ontbreekt": ontbreekt,
                    "database": database_melding,
                })
                return
            if is_hosted() and _ontbrekende_instellingen(target):
                self._setup_page()
                return
            if path == "/login":
                self._login_page()
                return
            if not self._logged_in():
                # Een API-verzoek hoort een nette 401 te krijgen, geen
                # inlogpagina met status 200 die een client als data leest.
                if path.startswith("/api/"):
                    self._json({"fout": "niet ingelogd"}, 401)
                else:
                    self._login_page()
                return
            if path in {"/", "/index.html"}:
                self._send(
                    200,
                    dashboard_pagina().replace("__TOKEN__", token).encode(),
                    "text/html; charset=utf-8",
                )
                return
            if not path.startswith("/api/"):
                self._json({"fout": "niet gevonden"}, 404)
                return

            store = self._store_of_fout()
            if store is None:
                return
            try:
                if path == "/api/overview":
                    self._json(_overview(store, campaign))
                elif path == "/api/leads":
                    self._json(_leads(store, query))
                elif path.startswith("/api/lead/"):
                    detail = _lead_detail(store, int(path.rsplit("/", 1)[1]))
                    self._json(detail or {"fout": "onbekende lead"}, 200 if detail else 404)
                elif path == "/api/outreach":
                    status = (query.get("status") or [""])[0]
                    sql = (
                        "SELECT o.*, l.name AS lead_name FROM outreach_log o "
                        "JOIN leads l ON l.id = o.lead_id "
                        + ("WHERE o.status = ? " if status else "")
                        + "ORDER BY o.id DESC LIMIT 300"
                    )
                    self._json(store.execute(sql, (status,) if status else ()))
                elif path == "/api/run/status":
                    with STATE_LOCK:
                        self._json({"running": STATE["running"], "log": list(STATE["log"])})
                else:
                    self._json({"fout": "niet gevonden"}, 404)
            except Exception as exc:  # noqa: BLE001
                self._json({"fout": str(exc)}, 400)
            finally:
                store.close()

        def do_POST(self) -> None:  # noqa: N802
            path, _query = self._route()

            if path == "/api/cron":
                self._cron()
                return

            if path == "/login":
                length = int(self.headers.get("Content-Length", 0) or 0)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                given = (form.get("wachtwoord") or [""])[0]
                if dashboard_password() and secrets.compare_digest(given, dashboard_password()):
                    cookie = (
                        f"lm_sessie={make_session_cookie()}; Path=/; HttpOnly; SameSite=Lax; "
                        f"Max-Age={SESSION_HOURS * 3600}"
                        + ("; Secure" if os.environ.get("VERCEL") else "")
                    )
                    self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
                else:
                    self._login_page(fout=True)
                return

            if not self._authorised():
                self._json({"fout": "niet ingelogd of token klopt niet"}, 403)
                return

            store = self._store_of_fout()
            if store is None:
                return
            try:
                if path == "/api/run":
                    if is_hosted():
                        # Een achtergronddraadje overleeft het antwoord niet in
                        # een serverless omgeving: zodra de functie klaar is,
                        # wordt alles opgeruimd. Dus draaien we hier binnen het
                        # verzoek, met hetzelfde tijdsbudget als de cron.
                        budget = float(os.environ.get("CRON_BUDGET_SECONDS", "35"))
                        counters = run_cycle(
                            campaign, store, trigger="dashboard",
                            budget_seconds=budget, report=_log,
                        )
                        self._json({"klaar": True, **counters})
                    else:
                        _run_in_background(campaign, target, "dashboard")
                        self._json({"gestart": True})
                elif path == "/api/send-now":
                    sent, failed = send_due(campaign, store, live=True, report=_log)
                    store.commit()
                    self._json({"verstuurd": sent, "mislukt": failed})
                elif path.startswith("/api/lead/"):
                    self._lead_action(store, path)
                elif path.startswith("/api/outreach/"):
                    self._outreach_action(store, path)
                else:
                    self._json({"fout": "niet gevonden"}, 404)
            except Exception as exc:  # noqa: BLE001
                self._json({"fout": str(exc)}, 400)
            finally:
                store.close()

        def _lead_action(self, store: Store, path: str) -> None:
            _, _, _, lead_id, action = path.split("/", 4)
            row = store.one(
                "SELECT l.*, a.score, a.segment, a.findings, d.url AS demo_url, d.slug AS demo_slug "
                "FROM leads l LEFT JOIN audits a ON a.lead_id = l.id "
                "LEFT JOIN demos d ON d.lead_id = l.id WHERE l.id = ?",
                (int(lead_id),),
            )
            if not row:
                self._json({"fout": "onbekende lead"}, 404)
                return

            if action == "demo":
                slug, html = build_demo(row, campaign)
                database.record_demo(
                    store, row["id"], slug, url=demo_url_for(slug), html=html, versie=DEMO_VERSIE
                )
                store.commit()
                self._json({"demo": f"/demo/{slug}"})
            elif action == "queue":
                ok, reason = eligible(store, row, campaign)
                if not ok:
                    self._json({"fout": reason}, 400)
                    return
                settings = autopilot_settings(campaign)
                wait = 0 if settings["send_mode"] == "auto" else settings["review_hours"]
                send_after = stamp(now() + timedelta(hours=wait))
                message = draft_email(row, campaign, demo_url=row.get("demo_url"))
                database.queue_outreach(store, row["id"], message, "first", send_after)
                store.commit()
                self._json({"klaargezet": True, "verstuurt_na": send_after})
            elif action == "suppress":
                if row.get("email"):
                    database.suppress(store, row["email"], "via dashboard")
                store.execute(
                    "UPDATE outreach_log SET status = 'geannuleerd' WHERE lead_id = ? AND status = 'wacht'",
                    (row["id"],),
                )
                store.commit()
                self._json({"afgemeld": True})
            else:
                self._json({"fout": "onbekende actie"}, 404)

        def _outreach_action(self, store: Store, path: str) -> None:
            _, _, _, outreach_id, action = path.split("/", 4)
            if action == "cancel":
                database.mark_outreach(store, int(outreach_id), "geannuleerd", "handmatig")
                store.commit()
                self._json({"geannuleerd": True})
            elif action == "now":
                store.execute(
                    "UPDATE outreach_log SET send_after = ? WHERE id = ? AND status = 'wacht'",
                    (stamp(), int(outreach_id)),
                )
                store.commit()
                sent, failed = send_due(campaign, store, live=True, report=_log, limit=1)
                store.commit()
                self._json({"verstuurd": sent, "mislukt": failed})
            else:
                self._json({"fout": "onbekende actie"}, 404)

        def _serve_demo(self, relative: str) -> None:
            slug = urllib.parse.unquote(relative).strip("/").split("/")[0]
            store = self._store()
            try:
                row = database.demo_by_slug(store, slug)
            finally:
                store.close()
            if row and row.get("html"):
                self._send(200, row["html"].encode("utf-8"), "text/html; charset=utf-8")
                return
            # Lokaal kan de pagina ook nog gewoon op schijf staan.
            root = (OUT_DIR / "demos").resolve()
            target_file = (root / slug / "index.html").resolve()
            if target_file.is_relative_to(root) and target_file.is_file():
                self._send(200, target_file.read_bytes(), "text/html; charset=utf-8")
                return
            self._send(404, b"Deze voorbeeldpagina bestaat niet (meer).", "text/plain; charset=utf-8")

    return Handler


def serve(campaign: Campaign, target: str | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    # Een vast token uit de omgeving houdt een geopende pagina werkend over
    # herstarts heen; zonder dat is een verse per start prima.
    token = os.environ.get("DASHBOARD_TOKEN") or secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer((host, port), make_handler(campaign, target, token))
    print(f"Dashboard draait op http://{host}:{port}")
    if host not in {"127.0.0.1", "localhost"} and not dashboard_password():
        print("Let op: geen DASHBOARD_PASSWORD ingesteld terwijl je buiten deze computer luistert.")
    print("Stoppen met Ctrl-C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard gestopt.")
    finally:
        httpd.server_close()
