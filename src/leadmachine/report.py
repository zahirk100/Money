"""Belllijst en dagoverzicht."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from . import db as database
from .audit import top_pitches
from .config import OUT_DIR, Campaign
from .demo import TEMPLATE_DIR


def build_calllist(rows: list[dict[str, Any]], campaign: Campaign, demo_base: str = "") -> Path:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)), trim_blocks=True, lstrip_blocks=True
    )
    leads = []
    for row in rows:
        findings = json.loads(row["findings"] or "[]")
        address = " ".join(p for p in [row["street"], row["housenumber"]] if p)
        leads.append(
            {
                "name": row["name"],
                "score": row["score"],
                "segment": row["segment"],
                "phone": row["phone"],
                "niche": row["niche"],
                "address": address,
                "city": row["city"],
                "website": row["website"],
                "demo_url": row["demo_url"] or (
                    f"{demo_base.rstrip('/')}/demo/{row['demo_slug']}" if demo_base and row.get("demo_slug") else None
                ),
                "pitches": top_pitches(findings, limit=2),
            }
        )

    markdown = env.get_template("calllist.md.j2").render(
        today=f"{date.today():%d-%m-%Y}",
        area=campaign.area,
        leads=leads,
        sender=campaign.outreach,
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"belllijst-{date.today():%Y%m%d}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def stats(store: Any) -> dict[str, Any]:
    def count(sql: str, *params: Any) -> int:
        return int(store.scalar(sql, params) or 0)

    per_segment = {
        row["segment"]: row["n"]
        for row in store.execute("SELECT segment, COUNT(*) AS n FROM audits GROUP BY segment")
    }
    per_niche = store.execute(
        "SELECT l.niche, COUNT(*) AS n, CAST(ROUND(AVG(a.score)) AS INTEGER) AS avg_score "
        "FROM leads l JOIN audits a ON a.lead_id = l.id "
        "GROUP BY l.niche ORDER BY avg_score DESC"
    )
    return {
        "leads": count("SELECT COUNT(*) AS n FROM leads"),
        "audited": count("SELECT COUNT(*) AS n FROM audits"),
        "with_email": count("SELECT COUNT(*) AS n FROM leads WHERE email IS NOT NULL AND email <> ''"),
        "with_phone": count("SELECT COUNT(*) AS n FROM leads WHERE phone IS NOT NULL AND phone <> ''"),
        "no_website": count("SELECT COUNT(*) AS n FROM leads WHERE website IS NULL OR website = ''"),
        "demos": count("SELECT COUNT(*) AS n FROM demos"),
        "drafted": count("SELECT COUNT(*) AS n FROM outreach_log WHERE status = 'concept'"),
        "queued": count("SELECT COUNT(*) AS n FROM outreach_log WHERE status = 'wacht'"),
        "sent": count("SELECT COUNT(*) AS n FROM outreach_log WHERE status = 'verstuurd'"),
        "failed": count("SELECT COUNT(*) AS n FROM outreach_log WHERE status = 'mislukt'"),
        "sent_today": database.sent_today(store),
        "per_segment": per_segment,
        "per_niche": per_niche,
    }
