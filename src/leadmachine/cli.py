"""Command line interface van de lead-machine.

    python -m leadmachine discover --source fixture
    python -m leadmachine audit --offline
    python -m leadmachine demo --top 20
    python -m leadmachine draft --top 20
    python -m leadmachine send --confirm --limit 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

from . import db as database
from . import report
from .audit import audit_lead, top_pitches
from .config import OUT_DIR, Campaign, ConfigError, load_campaign, load_dotenv
from .store import resolve_target
from .demo import build_demo, render_demo
from .http import PoliteClient
from .dashboard import serve
from .pipeline import autopilot_settings, run_cycle, send_due
from .outreach import (
    Mailer,
    OutreachError,
    draft_email,
    eligible,
    load_suppression_file,
    throttle,
    write_draft,
)

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _color(segment: str) -> str:
    return {"hot": RED, "warm": YELLOW}.get(segment, DIM)


def _client(campaign: Campaign) -> PoliteClient:
    audit_cfg = campaign.audit
    return PoliteClient(
        user_agent=str(audit_cfg.get("user_agent", "LeadMachine/1.0")),
        timeout=float(audit_cfg.get("timeout_seconds", 12)),
        delay=float(audit_cfg.get("delay_seconds", 1.5)),
        respect_robots=bool(audit_cfg.get("respect_robots", True)),
    )


def cmd_init(args: argparse.Namespace) -> int:
    load_dotenv()
    with database.session(args.db) as store:
        soort = "Supabase/Postgres" if store.dialect == "postgres" else "SQLite"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{GREEN}Database klaar ({soort}):{RESET} {_masked(resolve_target(args.db))}")
    print(f"{GREEN}Uitvoermap klaar:{RESET} {OUT_DIR}")
    return 0


def _masked(target: str) -> str:
    """Een databank-URL bevat een wachtwoord; dat hoeft niet in beeld."""
    return re.sub(r"://[^@/]*@", "://***@", target)


def cmd_discover(args: argparse.Namespace) -> int:
    from .discover import discover

    campaign = load_campaign(args.config)
    added = updated = 0
    with database.session(args.db) as store:
        for index, lead in enumerate(discover(campaign, source=args.source, only_niche=args.niche)):
            if args.limit and index >= args.limit:
                break
            _, is_new = database.upsert_lead(store, lead)
            added += int(is_new)
            updated += int(not is_new)
    print(f"{GREEN}{added} nieuwe leads{RESET}, {updated} bijgewerkt in {campaign.area}.")
    if added or updated:
        print(f"{DIM}Volgende stap: python -m leadmachine audit{RESET}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    campaign = load_campaign(args.config)
    client = None if args.offline else _client(campaign)
    with database.session(args.db) as store:
        rows = (
            store.execute("SELECT * FROM leads ORDER BY id")
            if args.refresh
            else database.leads_without_audit(store, args.limit)
        )
        if args.refresh and args.limit:
            rows = rows[: args.limit]
        if not rows:
            print("Niets te auditen. Draai eerst 'discover' of gebruik --refresh.")
            return 0
        for row in rows:
            result = audit_lead(row, client=client, offline=args.offline)
            result["segment"] = campaign.segment(result["score"])
            database.save_audit(store, row["id"], result)
            colour = _color(result["segment"])
            print(f"{colour}{result['score']:>3}{RESET}  {row['name'][:44]:<44} {result['segment']}")
    print(f"\n{GREEN}{len(rows)} bedrijven beoordeeld.{RESET}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    campaign = load_campaign(args.config)
    campaign.require_sender()
    load_dotenv()
    base_url = os.environ.get("DEMO_BASE_URL", "").rstrip("/")
    with database.session(args.db) as store:
        rows = database.ranked_leads(store, limit=args.top, segment=args.segment, niche=args.niche)
        for row in rows:
            slug, html = build_demo(row, campaign)
            path = render_demo(row, campaign)
            url = f"{base_url}/demo/{slug}" if base_url else None
            database.record_demo(store, row["id"], slug, path=str(path), url=url, html=html)
            print(f"{GREEN}gemaakt{RESET}  {row['name'][:40]:<40} {path}")
    print(f"\n{len(rows)} demo's in {OUT_DIR / 'demos'}")
    if not base_url:
        print(f"{DIM}Tip: zet DEMO_BASE_URL in .env zodat de links in je mails kloppen.{RESET}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    load_dotenv()
    campaign = load_campaign(args.config)
    campaign.require_sender()
    base_url = os.environ.get("DEMO_BASE_URL", "").rstrip("/")
    with database.session(args.db) as store:
        load_suppression_file(store, campaign)
        rows = database.ranked_leads(
            store, limit=args.top, segment=args.segment, niche=args.niche, with_email=True
        )
        made = 0
        for row in rows:
            ok, reason = eligible(store, row, campaign)
            if not ok:
                print(f"{DIM}overslaan {row['name'][:36]:<36} {reason}{RESET}")
                continue
            demo_url = row.get("demo_url")
            if not row.get("demo_slug"):
                # Zonder demo is de mail waardeloos, dus maak hem alsnog.
                slug, html = build_demo(row, campaign)
                demo_path = render_demo(row, campaign)
                demo_url = f"{base_url}/demo/{slug}" if base_url else None
                database.record_demo(store, row["id"], slug, path=str(demo_path), url=demo_url, html=html)
            message = draft_email(row, campaign, template=args.template, demo_url=demo_url)
            path = write_draft(row, message)
            database.log_outreach(
                store, row["id"], channel="email", template=args.template,
                to_addr=message["to"], subject=message["subject"],
                body=message["body"], status="concept",
            )
            made += 1
            print(f"{GREEN}concept{RESET}  {row['name'][:36]:<36} {path.name}")
    print(f"\n{made} concepten in {OUT_DIR / 'outreach'}. Lees ze na voordat je verstuurt.")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    load_dotenv()
    campaign = load_campaign(args.config)
    campaign.require_sender()
    daily_limit = int(campaign.outreach.get("daily_limit", 25))

    with database.session(args.db) as store:
        load_suppression_file(store, campaign)
        already = database.sent_today(store)
        room = max(0, daily_limit - already)
        budget = min(room, args.limit) if args.limit else room
        if budget <= 0:
            print(f"{YELLOW}Dagelijkse limiet ({daily_limit}) al bereikt.{RESET}")
            return 0

        rows = database.ranked_leads(
            store, limit=budget * 3, segment=args.segment, niche=args.niche, with_email=True
        )
        queue = []
        for row in rows:
            ok, reason = eligible(store, row, campaign)
            if ok:
                queue.append(row)
            else:
                print(f"{DIM}overslaan {row['name'][:36]:<36} {reason}{RESET}")
            if len(queue) >= budget:
                break

        if not queue:
            print("Geen leads die aan de voorwaarden voldoen.")
            return 0

        if not args.confirm:
            print(f"\n{YELLOW}PROEFDRAAI - er wordt niets verstuurd.{RESET}")
            for row in queue:
                message = draft_email(row, campaign, template=args.template, demo_url=row["demo_url"])
                print(f"  -> {message['to']:<36} {message['subject']}")
            print(f"\n{len(queue)} mails klaar. Voeg --confirm toe om echt te versturen.")
            return 0

        sent = failed = 0
        try:
            with Mailer(campaign, live=True) as mailer:
                for index, row in enumerate(queue):
                    message = draft_email(row, campaign, template=args.template, demo_url=row["demo_url"])
                    try:
                        mailer.send(message["to"], message["subject"], message["body"])
                        database.log_outreach(
                            store, row["id"], channel="email", template=args.template,
                            to_addr=message["to"], subject=message["subject"],
                            body=message["body"], status="verstuurd",
                        )
                        store.commit()
                        sent += 1
                        print(f"{GREEN}verstuurd{RESET} {message['to']}")
                    except Exception as exc:  # noqa: BLE001 - alles loggen, doorgaan
                        database.log_outreach(
                            store, row["id"], channel="email", template=args.template,
                            to_addr=message["to"], subject=message["subject"],
                            status="mislukt", error=str(exc),
                        )
                        store.commit()
                        failed += 1
                        print(f"{RED}mislukt{RESET}   {message['to']}: {exc}")
                    if index < len(queue) - 1:
                        throttle(campaign)
        except OutreachError as exc:
            print(f"{RED}{exc}{RESET}")
            return 1
    print(f"\n{sent} verstuurd, {failed} mislukt. Vandaag totaal: {already + sent}/{daily_limit}.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Een volledige cyclus: zoeken, beoordelen, bouwen, opstellen, versturen."""
    campaign = load_campaign(args.config)
    campaign.require_sender()
    settings = autopilot_settings(campaign)
    live = True if args.confirm else (False if args.dry_run else None)

    with database.session(args.db) as store:
        counters = run_cycle(
            campaign, store, trigger=args.trigger, live=live,
            report=lambda line: print(f"{DIM}{line}{RESET}"),
            force_discover=args.discover,
            offline=args.offline,
        )

    print(
        f"\n{GREEN}Klaar.{RESET} {counters['discovered']} nieuw, {counters['audited']} beoordeeld, "
        f"{counters['demos']} demo's, {counters['queued']} klaargezet, {counters['sent']} verstuurd"
        + (f", {counters['failed']} mislukt" if counters["failed"] else "") + "."
    )
    if not settings["enabled"] and not args.confirm:
        print(f"{DIM}Versturen stond uit. Zet autopilot.enabled op true of gebruik --confirm.{RESET}")
    return 0


def cmd_autopilot(args: argparse.Namespace) -> int:
    """Draait de cyclus elke dag op het ingestelde tijdstip."""
    campaign = load_campaign(args.config)
    campaign.require_sender()
    settings = autopilot_settings(campaign)

    if args.once:
        with database.session(args.db) as store:
            run_cycle(campaign, store, trigger="autopilot",
                      report=lambda line: print(f"{DIM}{line}{RESET}"))
        return 0

    hour, _, minute = settings["run_at"].partition(":")
    target = dt_time(int(hour), int(minute or 0))
    print(f"{GREEN}Autopilot gestart.{RESET} Draait elke dag om {settings['run_at']}.")
    if not settings["enabled"]:
        print(f"{YELLOW}Let op: autopilot.enabled staat op false, dus er wordt niets verstuurd.{RESET}")
    print("Stoppen met Ctrl-C.\n")

    while True:
        now = datetime.now()
        next_run = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait = (next_run - now).total_seconds()
        print(f"{DIM}Volgende cyclus: {next_run:%d-%m-%Y %H:%M}{RESET}")
        time.sleep(wait)
        try:
            with database.session(args.db) as store:
                run_cycle(campaign, store, trigger="autopilot",
                          report=lambda line: print(f"{DIM}{line}{RESET}"))
        except Exception as exc:  # noqa: BLE001 - morgen gewoon weer proberen
            print(f"{RED}Cyclus mislukt: {exc}{RESET}")


def cmd_dashboard(args: argparse.Namespace) -> int:
    campaign = load_campaign(args.config)
    load_dotenv()
    serve(campaign, args.db, host=args.host, port=args.port)
    return 0


def cmd_calllist(args: argparse.Namespace) -> int:
    load_dotenv()
    campaign = load_campaign(args.config)
    with database.session(args.db) as store:
        rows = database.ranked_leads(
            store, limit=args.top, segment=args.segment, niche=args.niche, with_phone=True
        )
        if not rows:
            print("Geen leads met telefoonnummer. Draai eerst discover + audit.")
            return 0
        path = report.build_calllist(rows, campaign, os.environ.get("DEMO_BASE_URL", ""))
    print(f"{GREEN}Belllijst met {len(rows)} bedrijven:{RESET} {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with database.session(args.db) as store:
        rows = database.ranked_leads(store, limit=args.top, segment=args.segment, niche=args.niche)
        for row in rows:
            colour = _color(row["segment"])
            pitch = top_pitches(json.loads(row["findings"] or "[]"), 1)
            print(
                f"{colour}{row['score']:>3}{RESET} {row['name'][:34]:<34} "
                f"{(row['niche'] or '')[:14]:<14} {(row['phone'] or '-')[:16]:<16} "
                f"{(row['email'] or '-')[:28]:<28}"
            )
            if args.verbose and pitch:
                print(f"     {DIM}{pitch[0]}{RESET}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with database.session(args.db) as store:
        data = report.stats(store)
    print(f"leads            {data['leads']}")
    print(f"  zonder website {data['no_website']}")
    print(f"  met telefoon   {data['with_phone']}")
    print(f"  met e-mail     {data['with_email']}")
    print(f"beoordeeld       {data['audited']}")
    for segment in ("hot", "warm", "cold"):
        print(f"  {segment:<14} {data['per_segment'].get(segment, 0)}")
    print(f"demo's           {data['demos']}")
    print(f"concepten        {data['drafted']}")
    print(f"verstuurd        {data['sent']} (vandaag {data['sent_today']})")
    if data["per_niche"]:
        print("\nper branche      aantal  gem. score")
        for row in data["per_niche"]:
            print(f"  {(row['niche'] or '-')[:16]:<16} {row['n']:>4}  {row['avg_score']:>10}")
    return 0


def cmd_suppress(args: argparse.Namespace) -> int:
    with database.session(args.db) as store:
        for value in args.value:
            database.suppress(store, value, args.reason)
            print(f"toegevoegd aan afmeldlijst: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leadmachine",
        description="Vind lokale bedrijven met een zwakke online aanwezigheid, "
                    "maak een voorbeeldsite en zet de outreach klaar.",
    )
    parser.add_argument(
        "--db", default=None,
        help="pad naar het databasebestand of een Postgres-URL "
             "(standaard: DATABASE_URL uit de omgeving, anders data/leads.db)",
    )
    parser.add_argument("--config", default=None, help="pad naar de campagne-config")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="database en mappen aanmaken").set_defaults(func=cmd_init)

    p = sub.add_parser("discover", help="bedrijven ophalen uit OpenStreetMap")
    p.add_argument("--source", choices=["overpass", "fixture"], default="overpass")
    p.add_argument("--niche", default=None, help="alleen deze niche")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("audit", help="websites beoordelen en scoren")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--refresh", action="store_true", help="ook al beoordeelde leads opnieuw")
    p.add_argument("--offline", action="store_true", help="niet fetchen, alleen op OSM-gegevens")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("demo", help="voorbeeldsites genereren")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--segment", choices=["hot", "warm", "cold"], default=None)
    p.add_argument("--niche", default=None)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("draft", help="mails als concept klaarzetten")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--segment", choices=["hot", "warm", "cold"], default=None)
    p.add_argument("--niche", default=None)
    p.add_argument("--template", choices=["first", "followup"], default="first")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("send", help="mails versturen (alleen met --confirm)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--segment", choices=["hot", "warm", "cold"], default=None)
    p.add_argument("--niche", default=None)
    p.add_argument("--template", choices=["first", "followup"], default="first")
    p.add_argument("--confirm", action="store_true", help="zonder deze vlag is het een proefdraai")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("calllist", help="belllijst met gespreksopeningen")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--segment", choices=["hot", "warm", "cold"], default=None)
    p.add_argument("--niche", default=None)
    p.set_defaults(func=cmd_calllist)

    p = sub.add_parser("list", help="leads op volgorde van score")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--segment", choices=["hot", "warm", "cold"], default=None)
    p.add_argument("--niche", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="een volledige cyclus draaien")
    p.add_argument("--confirm", action="store_true", help="versturen, ook als autopilot uit staat")
    p.add_argument("--dry-run", action="store_true", help="nooit versturen, wat de config ook zegt")
    p.add_argument("--discover", action="store_true", help="altijd opnieuw bedrijven ophalen")
    p.add_argument("--offline", action="store_true", help="websites niet ophalen, alleen op OSM-gegevens")
    p.add_argument("--trigger", default="handmatig", help="naam die in het overzicht komt te staan")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("autopilot", help="dagelijks automatisch draaien")
    p.add_argument("--once", action="store_true", help="een keer draaien en stoppen (voor cron)")
    p.set_defaults(func=cmd_autopilot)

    p = sub.add_parser("dashboard", help="het dashboard openen in je browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.set_defaults(func=cmd_dashboard)

    sub.add_parser("stats", help="overzicht van de pijplijn").set_defaults(func=cmd_stats)

    p = sub.add_parser("suppress", help="adres of domein op de afmeldlijst zetten")
    p.add_argument("value", nargs="+")
    p.add_argument("--reason", default="verzoek")
    p.set_defaults(func=cmd_suppress)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"{RED}Configuratiefout:{RESET} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAfgebroken.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
