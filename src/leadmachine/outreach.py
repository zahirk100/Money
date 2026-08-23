"""Outreach opstellen en (pas na expliciete bevestiging) versturen.

Uitgangspunten die hier in code staan, niet in een handleiding:
- niets gaat de deur uit zonder --confirm; standaard is alles een concept;
- harde dagelijkse limiet en pauze tussen verzendingen;
- afmeldregel en afzendergegevens zitten verplicht in elke mail;
- wie op de afmeldlijst staat, wordt overgeslagen;
- dezelfde onderneming niet opnieuw binnen de cooldown.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import sqlite3
import ssl
import textwrap
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .audit import top_pitches
from .config import OUT_DIR, REPO_ROOT, Campaign
from .demo import TEMPLATE_DIR

DRAFT_DIR = OUT_DIR / "outreach"


class OutreachError(Exception):
    pass


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _salutation(lead: Any) -> str:
    """Zonder naam van de eigenaar is 'team X' geloofwaardiger dan 'heer/mevrouw'."""
    name = lead["name"] if "name" in lead.keys() else "daar"
    return f"team {name}"


def _opener(lead: Any, pitches: list[str], niche_label: str) -> str:
    city = lead["city"] if "city" in lead.keys() else None
    where = f" in {city}" if city else ""
    first = pitches[0] if pitches else "er online nog weinig over jullie te vinden is"
    # Alleen verkleinen als de zin niet met een eigennaam begint.
    if first and not first.startswith(str(lead["name"])):
        first = first[0].lower() + first[1:]
    return (
        f"Ik zocht deze week naar een {niche_label}{where} en kwam {lead['name']} tegen. "
        f"Wat me opviel: {first}"
    )


def wrap_paragraphs(text: str, width: int = 72) -> str:
    """Breekt lopende alinea's af op 72 tekens; lijsten, links en de
    ondertekening blijven staan zoals ze zijn."""
    blocks = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        keep = lines[0].startswith(("Met vriendelijke groet", "SUBJECT:")) or any(
            line.lstrip().startswith(("-", ">", "http")) for line in lines
        )
        if keep:
            blocks.append("\n".join(lines))
        else:
            joined = " ".join(line.strip() for line in lines)
            blocks.append(textwrap.fill(joined, width=width))
    return "\n\n".join(blocks)


def draft_email(
    lead: sqlite3.Row | dict[str, Any],
    campaign: Campaign,
    template: str = "first",
    demo_url: str | None = None,
) -> dict[str, str]:
    keys = lead.keys() if hasattr(lead, "keys") else lead.keys()
    get = lambda k, d=None: lead[k] if k in keys else d  # noqa: E731

    findings = json.loads(get("findings") or "[]")
    pitches = top_pitches(findings, limit=3)
    niche_obj = campaign.niche(get("niche") or "")
    niche_label = niche_obj.label if niche_obj else "bedrijf"

    subject = (
        f"Voorbeeldpagina voor {get('name')}"
        if template == "first"
        else f"Nog even over die pagina voor {get('name')}"
    )

    price = f"vanaf {int(campaign.offer.get('price_from', 750))} euro"
    tpl = _env().get_template(f"email_{template}.txt.j2")
    rendered = tpl.render(
        subject=subject,
        salutation=_salutation(lead),
        opener=_opener(lead, pitches, niche_label),
        pitches=pitches[1:],
        lead={
            "name": get("name"), "city": get("city"),
            "phone": get("phone"), "opening_hours": get("opening_hours"),
        },
        demo_url=demo_url or get("demo_url") or "(demo nog niet gepubliceerd)",
        offer={**campaign.offer, "turnaround_days": campaign.offer.get("turnaround_days", 7)},
        price=price,
        sender=campaign.outreach,
    )

    header, _, body = rendered.partition("---\n")
    subject_line = header.replace("SUBJECT:", "").strip() or subject
    return {
        "subject": subject_line,
        "body": wrap_paragraphs(body.strip()) + "\n",
        "to": get("email") or "",
    }


def load_suppression_file(conn: sqlite3.Connection, campaign: Campaign) -> int:
    """Regels uit het afmeldbestand (een adres of domein per regel) in de db."""
    from .db import suppress

    path = campaign.outreach.get("suppression_file")
    if not path:
        return 0
    file = Path(path)
    if not file.is_absolute():
        file = REPO_ROOT / file
    if not file.exists():
        return 0
    count = 0
    for line in file.read_text(encoding="utf-8").splitlines():
        value = line.strip().lower()
        if value and not value.startswith("#"):
            suppress(conn, value, "afmeldbestand")
            count += 1
    return count


def eligible(conn: sqlite3.Connection, lead: sqlite3.Row, campaign: Campaign) -> tuple[bool, str]:
    from .db import is_suppressed, last_contact

    email = (lead["email"] if "email" in lead.keys() else "") or ""
    if not email or "@" not in email:
        return False, "geen mailadres bekend"
    if is_suppressed(conn, email):
        return False, "staat op de afmeldlijst"
    previous = last_contact(conn, lead["id"])
    if previous:
        cooldown = int(campaign.outreach.get("cooldown_days", 90))
        try:
            last_dt = datetime.fromisoformat(previous)
        except ValueError:
            return False, "eerder benaderd"
        if datetime.utcnow() - last_dt < timedelta(days=cooldown):
            return False, f"al benaderd op {last_dt:%d-%m-%Y} (cooldown {cooldown} dagen)"
    return True, ""


def write_draft(lead: sqlite3.Row, message: dict[str, str]) -> Path:
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w-]+", "-", (lead["name"] or "lead").lower()).strip("-")
    path = DRAFT_DIR / f"{date.today():%Y%m%d}-{lead['id']:05d}-{safe}.txt"
    path.write_text(
        f"Aan: {message['to'] or '(geen mailadres - bellen)'}\n"
        f"Onderwerp: {message['subject']}\n\n{message['body']}",
        encoding="utf-8",
    )
    return path


class Mailer:
    """Dunne SMTP-wrapper. Verstuurt alleen als hij expliciet is aangezet."""

    def __init__(self, campaign: Campaign, live: bool = False) -> None:
        self.campaign = campaign
        self.live = live
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.starttls = os.environ.get("SMTP_STARTTLS", "true").lower() != "false"
        self._smtp: smtplib.SMTP | None = None

    def __enter__(self) -> "Mailer":
        if self.live:
            if not (self.host and self.user and self.password):
                raise OutreachError(
                    "SMTP_HOST, SMTP_USER en SMTP_PASSWORD ontbreken. "
                    "Zet ze in .env (zie .env.example)."
                )
            self._smtp = smtplib.SMTP(self.host, self.port, timeout=30)
            self._smtp.ehlo()
            if self.starttls:
                self._smtp.starttls(context=ssl.create_default_context())
                self._smtp.ehlo()
            self._smtp.login(self.user, self.password)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._smtp:
            try:
                self._smtp.quit()
            except smtplib.SMTPException:
                pass

    def send(self, to_addr: str, subject: str, body: str) -> None:
        out = self.campaign.outreach
        message = EmailMessage()
        message["From"] = f"{out['sender_name']} <{out['sender_email']}>"
        message["To"] = to_addr
        message["Subject"] = subject
        if out.get("reply_to"):
            message["Reply-To"] = str(out["reply_to"])
        message.set_content(body)
        if not self.live or self._smtp is None:
            raise OutreachError("Mailer staat niet live; gebruik --confirm.")
        self._smtp.send_message(message)


def throttle(campaign: Campaign) -> None:
    time.sleep(float(campaign.outreach.get("seconds_between_sends", 45)))
