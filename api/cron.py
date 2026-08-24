"""Ingang voor de cron van Vercel: draait een stukje van de cyclus.

Een serverless functie mag maar beperkt lang draaien, dus dit doet nooit alles
in een keer. Elke aanroep werkt binnen een tijdsbudget en stopt netjes; de
volgende aanroep pakt op waar deze ophield, want de voortgang staat in de
database. Daarom mag deze cron gerust vaak draaien.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from leadmachine.config import load_campaign  # noqa: E402
from leadmachine.pipeline import run_cycle  # noqa: E402
from leadmachine.store import open_store  # noqa: E402

# Hoeveel seconden een aanroep zichzelf gunt. Zet maxDuration in vercel.json
# altijd ruimer dan dit getal.
BUDGET = float(os.environ.get("CRON_BUDGET_SECONDS", "45"))


def _toegestaan(headers) -> bool:
    """Vercel stuurt CRON_SECRET mee als Bearer-token. Staat er geen secret
    ingesteld, dan is het eindpunt open en dat wil je niet."""
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return False
    given = headers.get("Authorization", "")
    return given == f"Bearer {secret}" or headers.get("X-Cron-Secret", "") == secret


class handler(BaseHTTPRequestHandler):
    def _antwoord(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not _toegestaan(self.headers):
            self._antwoord(401, {"fout": "CRON_SECRET ontbreekt of klopt niet"})
            return

        store = open_store(os.environ.get("DATABASE_URL"))
        try:
            counters = run_cycle(
                load_campaign(), store, trigger="cron", budget_seconds=BUDGET
            )
            self._antwoord(200, {"status": "klaar", **counters})
        except Exception as exc:  # noqa: BLE001 - een mislukte run mag geen 500 zonder uitleg zijn
            traceback.print_exc()
            self._antwoord(500, {"status": "mislukt", "fout": str(exc)})
        finally:
            store.close()

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()
