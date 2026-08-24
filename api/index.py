"""Ingang voor Vercel: een functie die alles bedient.

Vercel wil een entrypoint dat het statisch kan vinden, dus staat hier een
echte klassedefinitie. Een toewijzing als handler = make_handler(...) is pas
bij het draaien een klasse en wordt daarom niet herkend.

Het pad naar dit entrypoint staat in pyproject.toml onder [tool.vercel].
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from leadmachine.config import load_campaign  # noqa: E402
from leadmachine.dashboard import make_handler  # noqa: E402

# Het token beschermt tegen verzoeken van andere websites. Het hoort per
# omgeving vast te staan; anders krijgt elke serverless instantie een ander
# token en werkt de knop in een al geopende pagina niet meer.
TOKEN = os.environ.get("DASHBOARD_TOKEN") or secrets.token_urlsafe(24)

_Dashboard = make_handler(load_campaign(), os.environ.get("DATABASE_URL"), TOKEN)


class handler(_Dashboard):  # noqa: N801 - Vercel zoekt precies deze naam
    """Het dashboard, de API, de demopagina's en de cron in een functie."""
