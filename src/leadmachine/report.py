"""Belllijst en dagoverzicht."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .audit import top_pitches
from .config import OUT_DIR, Campaign
from .demo import TEMPLATE_DIR


def build_calllist(rows: list[sqlite3.Row], campaign: Campaign, demo_base: str = "") -> Path:
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
                    f"{demo_base.rstrip('/')}/{Path(row['demo_path']).parent.name}/"
                    if demo_base and row["demo_path"] else None
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


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def one(sql: str, *params: Any) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0)

    per_segment = {
        row["segment"]: row["n"]
        for row in conn.execute(
            "SELECT segment, COUNT(*) AS n FROM audits GROUP BY segment"
        ).fetchall()
    }
    per_niche = [
        dict(row)
        for row in conn.execute(
            "SELECT l.niche, COUNT(*) AS n, CAST(ROUND(AVG(a.score)) AS INTEGER) AS avg_score "
            "FROM leads l JOIN audits a ON a.lead_id = l.id "
            "GROUP BY l.niche ORDER BY avg_score DESC"
        ).fetchall()
    ]
    return {
        "leads": one("SELECT COUNT(*) FROM leads"),
        "audited": one("SELECT COUNT(*) FROM audits"),
        "with_email": one("SELECT COUNT(*) FROM leads WHERE email IS NOT NULL AND email != ''"),
        "with_phone": one("SELECT COUNT(*) FROM leads WHERE phone IS NOT NULL AND phone != ''"),
        "no_website": one("SELECT COUNT(*) FROM leads WHERE website IS NULL OR website = ''"),
        "demos": one("SELECT COUNT(*) FROM demos"),
        "drafted": one("SELECT COUNT(*) FROM outreach_log WHERE status = 'drafted'"),
        "sent": one("SELECT COUNT(*) FROM outreach_log WHERE status = 'sent'"),
        "sent_today": one(
            "SELECT COUNT(*) FROM outreach_log WHERE status = 'sent' AND date(created_at) = date('now')"
        ),
        "per_segment": per_segment,
        "per_niche": per_niche,
    }
