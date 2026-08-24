"""Ingang voor Vercel: het dashboard en de API als serverless functie.

Vercel zoekt in een bestand onder api/ naar een klasse die handler heet. Dat is
precies wat het dashboard al is, dus hier hoeft alleen het pad naar de code
goedgezet te worden en de handler aangemaakt.
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

handler = make_handler(load_campaign(), os.environ.get("DATABASE_URL"), TOKEN)
