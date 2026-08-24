"""Scoort hoeveel online-pijn een bedrijf heeft. Hoge score = goede prospect.

Elke bevinding levert punten op en een zin die je letterlijk in je mail of
telefoongesprek kunt gebruiken. Dat is het hele punt: de audit schrijft je
openingszin voor je.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from .http import Fetched, PoliteClient

SOCIAL_ONLY = re.compile(r"(facebook\.com|instagram\.com|linktr\.ee|linkedin\.com)", re.I)
PARKED = re.compile(
    r"(under construction|in aanbouw|deze domeinnaam|domain (is )?for sale|"
    r"binnenkort online|coming soon|standaard(pagina| website)|welcome to nginx|apache2 default)",
    re.I,
)
PLATFORMS = {
    "wordpress": re.compile(r"wp-content|wp-includes|generator\" content=\"WordPress", re.I),
    "wix": re.compile(r"wix\.com|wixstatic", re.I),
    "squarespace": re.compile(r"squarespace", re.I),
    "jimdo": re.compile(r"jimdo", re.I),
    "shopify": re.compile(r"cdn\.shopify", re.I),
    "webflow": re.compile(r"webflow", re.I),
}
COPYRIGHT = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)
EMAIL_IN_PAGE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE_IN_PAGE = re.compile(r"(?:\+31|0)\s?\d[\d\s\-]{7,}")


def _finding(code: str, weight: int, pitch: str, detail: str | None = None) -> dict[str, Any]:
    """pitch = de zin die je richting de ondernemer gebruikt.
    detail = de technische toelichting, alleen voor jezelf en het dashboard."""
    finding = {"code": code, "weight": weight, "pitch": pitch}
    if detail:
        finding["detail"] = detail
    return finding


def audit_lead(
    lead: dict[str, Any] | Any,
    client: PoliteClient | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    get = lead.get if isinstance(lead, dict) else lambda k, d=None: lead[k] if k in lead.keys() else d
    website = (get("website") or "").strip()

    result: dict[str, Any] = {
        "reachable": None, "final_url": None, "status_code": None, "https": None,
        "mobile_ready": None, "title": None, "description": None, "load_ms": None,
        "html_bytes": None, "has_contact": None, "copyright_year": None,
        "platform": None, "findings": [], "score": 0,
    }
    findings: list[dict[str, Any]] = []

    if not website:
        findings.append(_finding(
            "geen_website", 55,
            "er is online geen eigen website te vinden; wie in Google zoekt, "
            "komt bij een concurrent uit."))
        result["findings"] = findings
        result["score"] = _total(findings)
        return result

    if SOCIAL_ONLY.search(website):
        findings.append(_finding(
            "alleen_social", 38,
            "de enige online plek is een socialmediapagina - je bent afhankelijk "
            "van dat platform en je bent slecht vindbaar in Google."))

    if website.startswith("http://"):
        findings.append(_finding(
            "geen_https", 12,
            "de site heeft geen beveiligde verbinding (https); browsers tonen "
            "bezoekers daardoor een waarschuwing."))

    if offline or client is None:
        result["findings"] = findings
        result["score"] = _total(findings)
        return result

    fetched = client.get(website)
    result.update({
        "reachable": int(fetched.ok),
        "final_url": fetched.final_url,
        "status_code": fetched.status_code,
        "load_ms": fetched.elapsed_ms,
        "html_bytes": fetched.bytes,
        "https": int(str(fetched.final_url).startswith("https://")),
    })

    if fetched.blocked_by_robots:
        result["findings"] = findings
        result["score"] = _total(findings)
        return result

    if not fetched.ok:
        if fetched.status_code:
            # De server heeft geantwoord, met een foutmelding. Dat is hard te
            # maken: wie de link opent krijgt hetzelfde te zien.
            findings.append(_finding(
                "site_geeft_fout", 42,
                f"wie de website opent krijgt een foutmelding ({fetched.status_code}) "
                "in plaats van de pagina.",
                detail=f"HTTP-status {fetched.status_code} op {fetched.final_url}"))
        else:
            # Geen antwoord. Dat kan van alles zijn: een hapering onderweg, een
            # firewall die servers weert, een trage verbinding. Wij weten alleen
            # dat WIJ er niet bij konden - niet dat de site stuk is. Daar mag
            # dus geen bewering over naar de ondernemer, en geen punten voor.
            findings.append(_finding(
                "niet_kunnen_controleren", 0,
                "we konden de website niet controleren vanaf onze server; "
                "dit zegt niets over de site zelf.",
                detail=f"geen antwoord: {fetched.error}. Handmatig nakijken voordat je contact opneemt."))
        result["findings"] = findings
        result["score"] = _total(findings)
        return result

    findings.extend(_analyse_html(fetched, result))
    result["findings"] = findings
    result["score"] = _total(findings)
    return result


def _analyse_html(fetched: Fetched, result: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    soup = BeautifulSoup(fetched.html, "html.parser")
    text = soup.get_text(" ", strip=True)[:20000]

    if PARKED.search(fetched.html[:6000]) or len(text) < 200:
        findings.append(_finding(
            "lege_site", 32,
            "op het domein staat nu alleen een lege of standaardpagina."))

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    result["title"] = title[:200] or None
    if not title:
        findings.append(_finding(
            "geen_titel", 8,
            "de pagina heeft geen titel, waardoor Google niet weet waarvoor je gevonden moet worden."))
    elif len(title) < 15 or title.lower() in {"home", "index", "welkom", "startpagina"}:
        findings.append(_finding(
            "zwakke_titel", 6,
            f"de paginatitel is '{title}' - daar staat niet in wat je doet of waar je zit, "
            "dus zoekt niemand daarop."))

    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = (desc_tag.get("content") or "").strip() if desc_tag else ""
    result["description"] = description[:300] or None
    if not description:
        findings.append(_finding(
            "geen_omschrijving", 5,
            "er staat geen omschrijving onder je Google-vermelding, dus die ziet er kaal uit."))

    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    result["mobile_ready"] = int(bool(viewport))
    if not viewport:
        findings.append(_finding(
            "niet_mobiel", 14,
            "de site is niet gemaakt voor mobiel, terwijl ruim twee derde van je "
            "bezoekers op een telefoon kijkt."))

    if fetched.elapsed_ms > 3500:
        findings.append(_finding(
            "traag", 10,
            f"de site deed er {fetched.elapsed_ms / 1000:.1f} seconde over om te laden; "
            "bezoekers klikken dan vaak al weg."))
    elif fetched.elapsed_ms > 2000:
        findings.append(_finding(
            "matig_traag", 5,
            f"de laadtijd is {fetched.elapsed_ms / 1000:.1f} seconde - dat kan een stuk sneller."))

    has_email = bool(EMAIL_IN_PAGE.search(text)) or bool(soup.select_one('a[href^="mailto:"]'))
    has_phone = bool(PHONE_IN_PAGE.search(text)) or bool(soup.select_one('a[href^="tel:"]'))
    result["has_contact"] = int(has_email or has_phone)
    if not (has_email or has_phone):
        findings.append(_finding(
            "geen_contact", 12,
            "er staat geen telefoonnummer of mailadres op de pagina, dus bellen "
            "kan alleen wie het al kent."))
    elif not soup.select_one('a[href^="tel:"]'):
        findings.append(_finding(
            "geen_belknop", 4,
            "het telefoonnummer is op mobiel niet aanklikbaar om direct te bellen."))

    years = [int(y) for y in COPYRIGHT.findall(fetched.html)]
    if years:
        newest = max(years)
        result["copyright_year"] = newest
        stale = date.today().year - newest
        if stale >= 2:
            findings.append(_finding(
                "verouderd", 9,
                f"onderaan de site staat nog {newest} - dat wekt de indruk dat het "
                "bedrijf er niet meer is."))

    for platform, pattern in PLATFORMS.items():
        if pattern.search(fetched.html):
            result["platform"] = platform
            break

    if fetched.bytes > 1_500_000:
        findings.append(_finding(
            "zwaar", 6,
            "de pagina is erg zwaar, wat vooral op 4G merkbaar traag is."))

    return findings


def _total(findings: list[dict[str, Any]]) -> int:
    return min(100, sum(int(f["weight"]) for f in findings))


def top_pitches(findings: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """De zwaarste bevindingen, als kant-en-klare zinnen."""
    ordered = sorted(findings, key=lambda f: -int(f.get("weight", 0)))
    return [f["pitch"] for f in ordered[:limit]]
