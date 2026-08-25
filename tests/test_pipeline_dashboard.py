"""Tests voor de cyclus, de wachtrij en het dashboard. Volledig offline:
de bedrijven komen uit de fixture en er wordt nooit echt gemaild.

    python tests/test_pipeline_dashboard.py
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadmachine import db as database  # noqa: E402
from leadmachine.config import load_campaign  # noqa: E402
from leadmachine.dashboard import _overview, make_handler  # noqa: E402
from leadmachine.pipeline import autopilot_settings, run_cycle, send_due  # noqa: E402
from leadmachine.store import now, stamp  # noqa: E402  # noqa: E402

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "campaign.example.yaml"

# De branches waarvan data/fixtures/sample_osm.json bedrijven bevat.
FIXTURE_BRANCHES = (
    "kapper", "schoonheidssalon", "aannemer", "installateur", "garage", "hovenier",
    "restaurant",
)


def campaign(**overrides):
    camp = load_campaign(EXAMPLE_CONFIG)
    camp._raw.setdefault("autopilot", {})
    camp._raw["autopilot"] = {**(camp._raw["autopilot"] or {}), "source": "fixture", **overrides}
    # Tests draaien op een vast gebied. De voorbeeldconfig staat op "auto", en
    # dan zou elke test langs 167 gemeenten willen: traag en onvoorspelbaar.
    camp.areas = ["Zwolle"]
    # En op de branches die ook echt in de fixture zitten. De echte config kent
    # er tientallen; die allemaal meenemen maakt de uitkomst afhankelijk van
    # welke branche toevallig vooraan in de rij staat, terwijl deze tests over
    # de cyclus gaan en niet over de brancheslijst.
    camp.niches = [n for n in camp.niches if n.name in FIXTURE_BRANCHES]
    return camp


class TestCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = database.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_cycle_fills_the_whole_pipeline(self):
        counters = run_cycle(campaign(), self.store, live=False, offline=True)
        self.assertGreater(counters["discovered"], 0)
        self.assertEqual(counters["audited"], counters["discovered"])
        self.assertGreater(counters["demos"], 0)
        self.assertGreater(counters["queued"], 0)
        # Zonder live=True gaat er niets de deur uit.
        self.assertEqual(counters["sent"], 0)

    def test_cycle_is_recorded_as_a_run(self):
        run_cycle(campaign(), self.store, trigger="test", live=False, offline=True)
        run = database.recent_runs(self.store, 1)[0]
        self.assertEqual(run["trigger"], "test")
        self.assertEqual(run["status"], "klaar")
        self.assertIsNotNone(run["finished_at"])

    def test_second_cycle_does_not_queue_the_same_lead_twice(self):
        first = run_cycle(campaign(), self.store, live=False, offline=True)
        second = run_cycle(campaign(), self.store, live=False, offline=True)
        self.assertGreater(first["queued"], 0)
        self.assertEqual(second["queued"], 0)

    def test_discovery_is_skipped_when_recent(self):
        """Een beurt doet een handvol zoekopdrachten. Is de hele rij gehad,
        dan hoort hij niet meteen opnieuw te beginnen."""
        for _ in range(6):
            run_cycle(campaign(), self.store, live=False, offline=True)
        laatste = run_cycle(campaign(), self.store, live=False, offline=True)
        self.assertEqual(laatste["discovered"], 0)

    def test_review_mode_delays_sending(self):
        run_cycle(campaign(send_mode="review", review_hours=12), self.store, live=False, offline=True)
        item = database.pending_outreach(self.store)[0]
        send_after = database._as_datetime(item["send_after"])
        self.assertGreater(send_after, now() + timedelta(hours=11))
        # Nog niets aan de beurt.
        self.assertEqual(database.due_outreach(self.store, 10), [])

    def test_auto_mode_queues_for_immediate_sending(self):
        run_cycle(campaign(send_mode="auto"), self.store, live=False, offline=True)
        self.assertTrue(database.due_outreach(self.store, 10))

    def test_min_score_keeps_weak_leads_out_of_the_queue(self):
        run_cycle(campaign(min_score=101), self.store, live=False, offline=True)
        self.assertEqual(database.pending_outreach(self.store), [])

    def test_daily_limit_stops_sending(self):
        camp = campaign(send_mode="auto")
        camp.outreach["daily_limit"] = 0
        run_cycle(camp, self.store, live=False, offline=True)
        sent, failed = send_due(camp, self.store, live=True)
        self.assertEqual((sent, failed), (0, 0))

    def test_suppressed_address_is_dropped_just_before_sending(self):
        camp = campaign(send_mode="auto")
        run_cycle(camp, self.store, live=False, offline=True)
        item = database.due_outreach(self.store, 1)[0]
        database.suppress(self.store, item["to_addr"], "test")
        self.store.commit()
        sent, _ = send_due(camp, self.store, live=True, limit=1)
        self.assertEqual(sent, 0)
        row = self.store.one("SELECT status FROM outreach_log WHERE id = ?", (item["id"],))
        self.assertEqual(row["status"], "geannuleerd")

    def test_settings_fall_back_to_safe_defaults(self):
        camp = load_campaign(EXAMPLE_CONFIG)
        camp._raw.pop("autopilot", None)
        settings = autopilot_settings(camp)
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["send_mode"], "review")


class TestDemoVernieuwing(unittest.TestCase):
    """Verandert het ontwerp, dan moeten bestaande demo's mee. Anders zit je
    vast aan de pagina's die je toevallig het eerst hebt gebouwd."""

    def test_demos_from_an_older_design_are_rebuilt_once(self):
        from leadmachine.demo import DEMO_VERSIE

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            run_cycle(campaign(), store, live=False, offline=True)
            aantal = int(store.scalar("SELECT COUNT(*) AS n FROM demos"))
            self.assertGreater(aantal, 0)

            # Een tweede beurt hoort niets opnieuw te bouwen.
            tweede = run_cycle(campaign(), store, live=False, offline=True)
            self.assertEqual(tweede["demos"], 0)

            # Nu doen alsof ze van een ouder ontwerp zijn.
            store.execute("UPDATE demos SET versie = ?", ("1",))
            store.commit()
            derde = run_cycle(campaign(), store, live=False, offline=True)
            self.assertEqual(derde["demos"], aantal, "alles hoort opnieuw gebouwd")
            self.assertEqual(
                store.scalar("SELECT COUNT(*) AS n FROM demos WHERE versie = ?", (DEMO_VERSIE,)),
                aantal,
            )

            # En daarna weer rustig blijven.
            self.assertEqual(run_cycle(campaign(), store, live=False, offline=True)["demos"], 0)
            store.close()

    def test_new_leads_get_a_demo_even_with_a_long_list_before_them(self):
        """Bedrijven zonder website krijgen allemaal dezelfde score. Werd de
        keuze op een top-N gemaakt, dan zat die top-N na een paar honderd leads
        vol met bedrijven die hun pagina al hadden en kwam er nooit meer een
        nieuwe aan de beurt."""
        from leadmachine.demo import DEMO_VERSIE

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            for i in range(200):
                lead_id, _ = database.upsert_lead(store, {
                    "osm_type": "node", "osm_id": str(9000 + i), "name": f"Bedrijf {i}",
                    "niche": "kapper", "city": "Zwolle", "source": "test",
                })
                database.save_audit(store, lead_id, {
                    "score": 55, "segment": "hot", "findings": [], "reachable": 0,
                })
                # De eerste 180 hebben hun pagina al.
                if i < 180:
                    database.record_demo(store, lead_id, f"bedrijf-{i}", versie=DEMO_VERSIE)
            store.commit()

            wachtenden = database.leads_needing_demo(
                store, limit=25, min_score=45, versie=DEMO_VERSIE
            )
            self.assertEqual(len(wachtenden), 20)
            self.assertTrue(all(row.get("demo_slug") is None for row in wachtenden))
            store.close()


class TestOverview(unittest.TestCase):
    def test_funnel_never_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            run_cycle(campaign(), store, live=False, offline=True)
            # Twee mails naar hetzelfde bedrijf mogen de trechter niet laten groeien.
            lead_id = store.one("SELECT lead_id FROM outreach_log LIMIT 1")["lead_id"]
            for _ in range(3):
                database.log_outreach(store, lead_id, status="verstuurd", to_addr="x@y.nl")
            store.commit()
            funnel = _overview(store, campaign())["funnel"]
            aantallen = [stap["aantal"] for stap in funnel]
            self.assertEqual(aantallen, sorted(aantallen, reverse=True))
            store.close()


class TestDashboardServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "t.db")
        store = database.connect(cls.db_path)
        run_cycle(campaign(), store, live=False, offline=True)
        store.close()

        cls.token = "test-token"
        handler = make_handler(campaign(), cls.db_path, cls.token)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read())

    def post(self, path, token=None):
        request = urllib.request.Request(self.base + path, method="POST")
        if token:
            request.add_header("X-LM-Token", token)
        with urllib.request.urlopen(request, timeout=5) as resp:
            return json.loads(resp.read())

    def test_page_serves_and_carries_the_token(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode()
        self.assertIn(f'data-token="{self.token}"', html)
        self.assertNotIn("__TOKEN__", html)

    def test_overview_reports_the_pipeline(self):
        data = self.get("/api/overview")
        self.assertGreater(data["stats"]["leads"], 0)
        self.assertEqual(len(data["sent_per_day"]), 14)
        self.assertEqual(len(data["funnel"]), 5)

    def test_leads_can_be_filtered(self):
        alle = self.get("/api/leads")
        kappers = self.get("/api/leads?niche=kapper")
        self.assertGreater(len(alle), len(kappers))
        self.assertTrue(all(lead["niche"] == "kapper" for lead in kappers))

    def test_list_also_shows_leads_that_are_not_audited_yet(self):
        """Net gevonden bedrijven horen zichtbaar te zijn, ook al is hun website
        nog niet bekeken. Anders lijkt het alsof er niets is gevonden."""
        store = database.connect(self.db_path)
        try:
            database.upsert_lead(store, {
                "osm_type": "node", "osm_id": "nieuw-1", "name": "Nog Niet Bekeken BV",
                "niche": "kapper", "city": "Zwolle",
            })
            store.commit()
        finally:
            store.close()

        namen = [lead["name"] for lead in self.get("/api/leads")]
        self.assertIn("Nog Niet Bekeken BV", namen)
        zonder_oordeel = [lead for lead in self.get("/api/leads") if lead["segment"] is None]
        self.assertTrue(zonder_oordeel)
        # Beoordeelde bedrijven horen wel bovenaan te staan.
        self.assertIsNotNone(self.get("/api/leads")[0]["segment"])
        # Een filter op segment laat ze juist weg.
        self.assertTrue(all(l["segment"] == "hot" for l in self.get("/api/leads?segment=hot")))

    def test_lead_row_carries_the_demo_slug(self):
        """De lijst bouwde de demolink uit een bestandspad. Online is er geen
        schijf, dus dat pad is leeg en liep de hele tabel stuk."""
        leads = self.get("/api/leads")
        met_demo = [lead for lead in leads if lead["heeft_demo"]]
        self.assertTrue(met_demo, "er hoort minstens een demo te zijn")
        for lead in met_demo:
            self.assertTrue(lead["demo_slug"], "zonder slug is de demo niet te openen")

    def test_branch_filter_lists_every_branch_in_the_database(self):
        """Ook branches waarvan nog niets beoordeeld is: juist daar zitten de
        bedrijven die je nog moet bekijken."""
        branches = self.get("/api/overview")["niches"]
        self.assertIn("kapper", branches)
        self.assertGreater(len(branches), 1)

    def test_search_matches_on_name(self):
        found = self.get("/api/leads?q=schaar")
        self.assertTrue(found)
        self.assertIn("schaar", found[0]["name"].lower())

    def test_write_actions_require_the_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/lead/1/demo")
        self.assertEqual(ctx.exception.code, 403)

    def test_write_action_works_with_the_token(self):
        result = self.post("/api/lead/1/demo", token=self.token)
        self.assertIn("demo", result)

    def test_demo_files_stay_inside_the_demo_folder(self):
        for attempt in ("/demo/..%2f..%2fconfig/campaign.example.yaml", "/demo/nietbestaand/"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + attempt, timeout=5)
            self.assertEqual(ctx.exception.code, 404)

    def test_rewritten_path_is_honoured(self):
        """Vercel stuurt alles naar een functie en geeft het echte pad mee als
        __lm_path. Zonder dat zou het dashboard op elke URL een 404 geven."""
        data = self.get("/api/index?__lm_path=/api/overview")
        self.assertGreater(data["stats"]["leads"], 0)

        with urllib.request.urlopen(self.base + "/api/index?__lm_path=/", timeout=5) as resp:
            self.assertIn(b"<!doctype html>", resp.read()[:40].lower())

    def test_rewritten_path_keeps_the_query(self):
        gefilterd = self.get("/api/index?__lm_path=/api/leads&niche=kapper")
        self.assertTrue(gefilterd)
        self.assertTrue(all(lead["niche"] == "kapper" for lead in gefilterd))

    def test_rewritten_path_works_for_write_actions(self):
        result = self.post("/api/index?__lm_path=/api/lead/1/demo", token=self.token)
        self.assertIn("demo", result)

    def test_cron_route_needs_the_secret(self):
        os.environ["CRON_SECRET"] = "cron-geheim"
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + "/api/cron", timeout=10)
            self.assertEqual(ctx.exception.code, 401)

            request = urllib.request.Request(
                self.base + "/api/cron", headers={"Authorization": "Bearer cron-geheim"})
            with urllib.request.urlopen(request, timeout=60) as resp:
                self.assertEqual(json.loads(resp.read())["status"], "klaar")
        finally:
            os.environ.pop("CRON_SECRET", None)

    def test_cron_route_is_shut_without_a_secret_configured(self):
        os.environ.pop("CRON_SECRET", None)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/cron", timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_unknown_route_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/bestaat-niet")
        self.assertEqual(ctx.exception.code, 404)


class TestVastgelopenDraaibeurten(unittest.TestCase):
    def test_a_timed_out_run_does_not_stay_busy_forever(self):
        """Kapt het platform de functie af, dan kan de draaibeurt zichzelf niet
        afsluiten. De volgende beurt hoort hem op afgebroken te zetten."""
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            oud = database.start_run(store, "cron")
            store.execute(
                "UPDATE runs SET started_at = ? WHERE id = ?",
                (stamp(now() - timedelta(hours=2)), oud),
            )
            vers = database.start_run(store, "cron")
            store.commit()

            database.close_stale_runs(store)
            store.commit()

            statussen = {r["id"]: r["status"] for r in database.recent_runs(store)}
            self.assertEqual(statussen[oud], "afgebroken")
            self.assertEqual(statussen[vers], "bezig", "een lopende beurt mag je niet afsluiten")
            store.close()

    def test_discovery_never_waits_longer_than_the_budget(self):
        """De aanroep naar Overpass moet opgeven voordat het platform de hele
        functie afkapt. Precies dat ging mis bij de eerste echte draaibeurt."""
        from leadmachine import pipeline

        gezien = {}

        def nep_discover(campaign, source="overpass", only_niche=None, timeout=30.0, **rest):
            gezien["timeout"] = timeout
            return iter(())

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                pipeline._discover_step(
                    campaign(), store, {"discover_every_days": 7, "source": "overpass"},
                    pipeline.Budget(20), lambda _: None, True,
                )
            finally:
                pipeline.discover = echte
            store.close()

        self.assertLess(gezien["timeout"], 20, "wachten mag nooit langer dan het budget")
        self.assertGreaterEqual(gezien["timeout"], 8)


class TestToegang(unittest.TestCase):
    """Het dashboard mag nooit per ongeluk open op het internet staan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        store = database.connect(self.db_path)
        run_cycle(campaign(), store, live=False, offline=True)
        store.close()
        self._oude_omgeving = {
            k: os.environ.get(k) for k in ("DASHBOARD_PASSWORD", "LM_HOSTED", "DATABASE_URL")
        }
        self.httpd = None

    def tearDown(self):
        for key, value in self._oude_omgeving.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.httpd is not None:
            # shutdown() blijft hangen als serve_forever() nooit is gestart.
            self.httpd.shutdown()
            self.httpd.server_close()
        self.tmp.cleanup()

    def _start(self, target: str | None = "__standaard__"):
        handler = make_handler(
            campaign(), self.db_path if target == "__standaard__" else target, "token"
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _status(self, url, **kwargs):
        try:
            with urllib.request.urlopen(url, timeout=5, **kwargs) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_hosted_without_password_stays_shut(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ.pop("DASHBOARD_PASSWORD", None)
        base = self._start()
        self.assertEqual(self._status(base + "/"), 503)
        self.assertEqual(self._status(base + "/api/overview"), 503)

    def test_setup_page_names_what_is_missing(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ.pop("DASHBOARD_PASSWORD", None)
        os.environ.pop("DATABASE_URL", None)
        base = self._start(target=None)
        try:
            urllib.request.urlopen(base + "/", timeout=5)
            self.fail("had een 503 moeten geven")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 503)
            pagina = exc.read().decode()
        self.assertIn("DATABASE_URL", pagina)
        self.assertIn("DASHBOARD_PASSWORD", pagina)

    def test_database_problems_are_explained_not_a_bare_500(self):
        """Een mislukte verbinding liep buiten alle foutafhandeling om en gaf
        een kale 500. Nu hoort er te staan wat je moet doen."""
        os.environ["LM_HOSTED"] = "1"
        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres.abc:[YOUR-PASSWORD]@aws-1-eu-central-1.pooler.supabase.com:5432/postgres"
        )
        base = self._start(target=None)

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.open(urllib.request.Request(base + "/login", data=b"wachtwoord=geheim"), timeout=5)
        try:
            opener.open(base + "/api/overview", timeout=15)
            self.fail("had een foutmelding moeten geven")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 503)
            melding = json.loads(exc.read())["fout"]
        self.assertIn("YOUR-PASSWORD", melding)

    def test_health_check_reports_the_database(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        os.environ.pop("DATABASE_URL", None)
        base = self._start()
        with urllib.request.urlopen(base + "/gezond", timeout=10) as resp:
            gezond = json.loads(resp.read())
        self.assertTrue(gezond["klaar"])
        self.assertIn("bereikbaar", gezond["database"])

    def test_health_check_reports_readiness(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ.pop("DASHBOARD_PASSWORD", None)
        base = self._start(target=None)
        with urllib.request.urlopen(base + "/gezond", timeout=5) as resp:
            self.assertFalse(json.loads(resp.read())["klaar"])

    def test_local_run_needs_no_database_url(self):
        """Zonder hosting valt hij gewoon terug op het bestand."""
        os.environ.pop("LM_HOSTED", None)
        os.environ.pop("DATABASE_URL", None)
        from leadmachine.store import resolve_target

        self.assertTrue(resolve_target().endswith(".db"))

    def test_hosted_without_database_url_is_a_clear_error(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ.pop("DATABASE_URL", None)
        from leadmachine.store import OpslagOntbreekt, resolve_target

        with self.assertRaises(OpslagOntbreekt) as ctx:
            resolve_target()
        self.assertIn("DATABASE_URL", str(ctx.exception))

    def test_demo_pages_stay_public_even_then(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ.pop("DASHBOARD_PASSWORD", None)
        base = self._start()
        store = database.connect(self.db_path)
        slug = store.one("SELECT slug FROM demos LIMIT 1")["slug"]
        store.close()
        self.assertEqual(self._status(f"{base}/demo/{slug}"), 200)

    def test_password_login_gives_access(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        base = self._start()
        self.assertEqual(self._status(base + "/api/overview"), 401)

        request = urllib.request.Request(
            base + "/login", data=b"wachtwoord=geheim", method="POST")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(request, timeout=5) as resp:
            self.assertEqual(resp.status, 200)  # volgt de omleiding naar /
        # De cookie uit dezelfde opener geeft nu toegang tot de API.
        with opener.open(base + "/api/overview", timeout=5) as resp:
            self.assertGreater(json.loads(resp.read())["stats"]["leads"], 0)

    def test_hosted_run_finishes_inside_the_request(self):
        """Serverless heeft geen achtergrond: een draadje wordt opgeruimd zodra
        het antwoord verstuurd is. De cyclus moet dus binnen het verzoek af."""
        os.environ["LM_HOSTED"] = "1"
        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        base = self._start()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.open(urllib.request.Request(base + "/login", data=b"wachtwoord=geheim"), timeout=5)

        request = urllib.request.Request(
            base + "/api/run", method="POST", headers={"X-LM-Token": "token"})
        with opener.open(request, timeout=120) as resp:
            uitkomst = json.loads(resp.read())
        self.assertTrue(uitkomst.get("klaar"))
        self.assertNotIn("gestart", uitkomst)

    def test_discovery_resumes_where_it_stopped(self):
        """Met een krap budget moet er toch vooruitgang zijn, en moet de rest
        onthouden worden in plaats van opnieuw te beginnen."""
        from leadmachine.pipeline import Budget, _discover_step

        store = database.connect(self.db_path)
        try:
            store.execute("DELETE FROM meta")
            store.execute("DELETE FROM leads")
            store.commit()
            camp = campaign()
            # Meer dan een gemeente, anders is er na de eerste opdracht niets
            # meer om te onthouden.
            camp.areas = ["Zwolle", "Kampen", "Deventer"]
            instellingen = {"discover_every_days": 7, "source": "fixture", "backlog_grens": 40}

            eerste = _discover_step(camp, store, instellingen, Budget(0.001), lambda _: None, False)
            openstaand = json.loads(database.get_meta(store, "discover_pending"))
            self.assertGreater(eerste, 0, "een krap budget mag niet betekenen: niets doen")
            self.assertTrue(openstaand, "de rest hoort onthouden te worden")

            _discover_step(camp, store, instellingen, Budget(None), lambda _: None, False)
            self.assertEqual(json.loads(database.get_meta(store, "discover_pending")), [])
            self.assertIsNotNone(database.get_meta(store, "last_discover"))
        finally:
            store.close()

    def test_search_covers_every_area_and_branch(self):
        """De machine hoort niet aan een stad vast te zitten: elke gemeente uit
        de config komt in de wachtrij. De branches zitten in een opdracht, dus
        er hoort er precies een per gemeente te zijn."""
        from leadmachine import pipeline

        gezien = []

        def nep_discover(campaign, source="overpass", only_niche=None, area=None, **rest):
            gezien.append((area, only_niche))
            return iter(())

        camp = campaign()
        camp.areas = ["Zwolle", "Kampen"]
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                # Een beurt doet een handvol zoekopdrachten; doorgaan tot de rij
                # leeg is. Alleen de eerste keer forceren, anders begint hij na
                # het leegwerken gewoon opnieuw.
                for beurt in range(8):
                    pipeline._discover_step(
                        camp, store,
                        {"discover_every_days": 7, "source": "overpass", "backlog_grens": 40},
                        pipeline.Budget(None), lambda _: None, beurt == 0,
                    )
                    if not json.loads(database.get_meta(store, "discover_pending") or "[]"):
                        break
            finally:
                pipeline.discover = echte
            store.close()

        gebieden = {gebied for gebied, _ in gezien}
        self.assertEqual(gebieden, {"Zwolle", "Kampen"})
        self.assertEqual(len(gezien), len(camp.areas), "een opdracht per gemeente")
        self.assertTrue(all(niche is None for _, niche in gezien),
                        "alle branches horen in dezelfde opdracht te zitten")

    def test_automatic_mode_picks_towns_itself(self):
        """Zonder opgegeven gemeenten hoort de machine zelf combinaties te
        trekken, zodat er altijd nieuwe leads te vinden zijn."""
        from leadmachine import pipeline
        from leadmachine.gemeenten import alle_gemeenten

        gezien = []

        def nep_discover(campaign, source="overpass", only_niche=None, area=None, **rest):
            gezien.append((area, only_niche))
            return iter(())

        camp = campaign()
        camp.areas = ["auto"]
        self.assertTrue(camp.automatisch)

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                for _ in range(5):
                    pipeline._discover_step(
                        camp, store,
                        {"discover_every_days": 7, "source": "overpass", "backlog_grens": 999},
                        pipeline.Budget(1.0), lambda _: None, False,
                    )
            finally:
                pipeline.discover = echte

            wachtrij = json.loads(database.get_meta(store, "discover_pending"))
            store.close()

        bezocht = {gebied for gebied, _ in gezien}
        self.assertTrue(bezocht <= set(alle_gemeenten()), "alleen bestaande gemeenten")
        self.assertGreater(len(gezien), 0)
        # Er blijft genoeg over om door te gaan.
        self.assertGreater(len(wachtrij), 100)

    def test_automatic_mode_keeps_its_order_between_runs(self):
        """Elke beurt opnieuw loten zou betekenen dat hij dezelfde gemeenten
        blijft trekken en andere nooit ziet."""
        from leadmachine import pipeline

        camp = campaign()
        camp.areas = ["auto"]

        def nep_discover(*args, **kwargs):
            return iter(())

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                instellingen = {"discover_every_days": 7, "source": "overpass", "backlog_grens": 999}
                pipeline._discover_step(camp, store, instellingen, pipeline.Budget(1.0), lambda _: None, False)
                eerste = json.loads(database.get_meta(store, "discover_pending"))[:5]
                pipeline._discover_step(camp, store, instellingen, pipeline.Budget(0.0001), lambda _: None, False)
                tweede = json.loads(database.get_meta(store, "discover_pending"))[:5]
            finally:
                pipeline.discover = echte
            store.close()

        self.assertEqual(eerste[1:], tweede[:4], "de rij hoort op te schuiven, niet opnieuw geschud")

    def test_adding_a_town_restarts_the_search(self):
        """Zonder deze controle bleef hij dezelfde gemeente doen: de wachtrij
        was leeg en 'klaar met zoeken' gold nog een week."""
        from leadmachine import pipeline

        gezien = []

        def nep_discover(campaign, source="overpass", only_niche=None, area=None, **rest):
            gezien.append(area)
            return iter(())

        camp = campaign()
        camp.areas = ["Zwolle"]
        instellingen = {"discover_every_days": 7, "source": "overpass", "backlog_grens": 999}

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                # Doorgaan tot alles gehad is; een beurt doet een handvol
                # zoekopdrachten, niet de hele rij.
                for _ in range(6):
                    pipeline._discover_step(
                        camp, store, instellingen, pipeline.Budget(None), lambda _: None, False)
                self.assertEqual(set(gezien), {"Zwolle"})

                # Niets veranderd: hij blijft rustig.
                gezien.clear()
                pipeline._discover_step(
                    camp, store, instellingen, pipeline.Budget(None), lambda _: None, False)
                self.assertEqual(gezien, [])

                # Gemeente erbij: die hoort er meteen bij te komen.
                gezien.clear()
                camp.areas = ["Zwolle", "Kampen"]
                for _ in range(8):
                    pipeline._discover_step(
                        camp, store, instellingen, pipeline.Budget(None), lambda _: None, False)
                self.assertIn("Kampen", gezien)
            finally:
                pipeline.discover = echte
            store.close()

    def test_an_old_queue_from_before_the_change_still_finishes(self):
        """De wachtrij bevatte kale branchenamen zonder gemeente."""
        from leadmachine import pipeline

        gezien = []

        def nep_discover(campaign, source="overpass", only_niche=None, area=None, **rest):
            gezien.append((area, only_niche))
            return iter(())

        camp = campaign()
        camp.areas = ["Zwolle", "Kampen"]
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            database.set_meta(store, "discover_pending", json.dumps(["garage", "hovenier"]))
            database.set_meta(store, "last_discover", stamp())
            store.commit()

            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                for _ in range(6):
                    pipeline._discover_step(
                        camp, store,
                        {"discover_every_days": 7, "source": "overpass", "backlog_grens": 999},
                        pipeline.Budget(None), lambda _: None, False,
                    )
            finally:
                pipeline.discover = echte

            self.assertEqual({gebied for gebied, _ in gezien}, {"Zwolle", "Kampen"})
            self.assertEqual(json.loads(database.get_meta(store, "discover_pending")), [])
            store.close()

    def test_branches_are_audited_in_turn(self):
        """Anders staat de lijst vol met de branche die toevallig als eerste
        werd opgehaald, en zie je de rest pas dagen later."""
        from leadmachine.pipeline import _om_en_om

        leads = ([{"niche": "kapper", "id": i} for i in range(4)]
                 + [{"niche": "garage", "id": i} for i in range(2)])
        volgorde = [lead["niche"] for lead in _om_en_om(leads)]
        self.assertEqual(volgorde[:4], ["kapper", "garage", "kapper", "garage"])
        self.assertEqual(len(volgorde), len(leads), "er mag niets wegvallen")
        self.assertEqual(_om_en_om([]), [])

    def test_the_cycle_passes_the_website_filter_along(self):
        from leadmachine import pipeline

        gezien = {}

        def nep_discover(campaign, source="overpass", only_niche=None, area=None,
                         alleen_zonder_website=None, **rest):
            gezien["filter"] = alleen_zonder_website
            return iter(())

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            echte = pipeline.discover
            pipeline.discover = nep_discover
            try:
                pipeline._discover_step(
                    campaign(), store,
                    {"discover_every_days": 7, "source": "overpass", "backlog_grens": 999,
                     "alleen_zonder_website": True},
                    pipeline.Budget(None), lambda _: None, True,
                )
            finally:
                pipeline.discover = echte
            store.close()

        self.assertTrue(gezien["filter"])

    def test_an_overpass_outage_does_not_kill_the_cycle(self):
        """Beoordelen en demo's bouwen hebben niets met Overpass te maken; die
        horen door te gaan als het ophalen stukloopt."""
        from leadmachine import pipeline

        def stukke_discover(*args, **kwargs):
            raise RuntimeError("Overpass onbereikbaar (timed out)")

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            run_cycle(campaign(), store, live=False, offline=True)   # eerst vullen
            store.execute("DELETE FROM audits")
            store.execute("DELETE FROM meta")
            store.commit()

            echte = pipeline.discover
            pipeline.discover = stukke_discover
            try:
                tellers = run_cycle(campaign(), store, live=False, offline=True, force_discover=True)
            finally:
                pipeline.discover = echte

            self.assertEqual(tellers["discovered"], 0)
            self.assertGreater(tellers["audited"], 0, "beoordelen hoort gewoon door te gaan")
            run = database.recent_runs(store, 1)[0]
            self.assertEqual(run["status"], "klaar", "een storing bij Overpass is geen mislukte beurt")
            store.close()

    def test_a_big_backlog_pauses_the_search(self):
        """Honderd te controleren websites zijn meer waard dan honderd nieuwe
        bedrijven. Bedrijven zonder website tellen hier niet mee: die zijn
        meteen beoordeeld en kosten dus geen tijd."""
        from leadmachine import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            for i in range(45):
                database.upsert_lead(store, {
                    "osm_type": "node", "osm_id": f"b{i}", "name": f"Bedrijf {i}", "niche": "kapper",
                    "website": f"https://bedrijf{i}.example.com",
                })
            store.commit()
            gevonden = pipeline._discover_step(
                campaign(), store, {"discover_every_days": 7, "source": "fixture", "backlog_grens": 40},
                pipeline.Budget(None), lambda _: None, False,
            )
            self.assertEqual(gevonden, 0)

            # Zonder website is het oordeel meteen klaar; zo'n stapel hoort het
            # ophalen niet tegen te houden.
            store.execute("UPDATE leads SET website = NULL")
            store.commit()
            gevonden = pipeline._discover_step(
                campaign(), store, {"discover_every_days": 7, "source": "fixture", "backlog_grens": 40},
                pipeline.Budget(None), lambda _: None, False,
            )
            self.assertGreater(gevonden, 0)
            store.close()

    def test_wrong_password_is_refused(self):
        os.environ["LM_HOSTED"] = "1"
        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        base = self._start()
        request = urllib.request.Request(
            base + "/login", data=b"wachtwoord=fout", method="POST")
        self.assertEqual(self._status(request), 401)

    def test_session_cookie_cannot_be_forged(self):
        from leadmachine.dashboard import make_session_cookie, valid_session

        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        echt = make_session_cookie()
        self.assertTrue(valid_session(echt))
        vervalst = echt.split(".")[0] + ".0000000000000000000000000000000"
        self.assertFalse(valid_session(vervalst))
        self.assertFalse(valid_session("9999999999.watdanook"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAiTekstInDeCyclus(unittest.TestCase):
    """De AI-tekst is optioneel. Staat hij aan, dan mag hij per bedrijf hoogstens
    een keer geld kosten; gaat hij stuk, dan draait de rest gewoon door."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = database.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    TEKST = {
        "kop": "Zelf gebakken, elke dag",
        "onderkop": "Uit onze eigen oven",
        "intro": "Wij bakken alles zelf. Loop gerust binnen.",
        "diensten": [
            {"titel": "Desembrood", "tekst": "Twee dagen rijzen."},
            {"titel": "Taart", "tekst": "Op bestelling."},
            {"titel": "Broodjes", "tekst": "Vanaf zeven uur."},
        ],
    }

    def test_off_by_default(self):
        from leadmachine.demo import DEMO_VERSIE

        run_cycle(campaign(), self.store, live=False, offline=True)
        versies = {r["versie"] for r in self.store.execute("SELECT versie FROM demos")}
        self.assertEqual(versies, {DEMO_VERSIE})

    def test_each_business_is_written_once_and_then_reused(self):
        from leadmachine import ai_tekst
        from leadmachine.demo import DEMO_VERSIE

        aanroepen = []

        def nep(lead, label, tags, timeout=25.0):
            aanroepen.append(lead["id"])
            return dict(self.TEKST)

        with mock.patch.object(ai_tekst, "ingeschakeld", return_value=True), \
                mock.patch.object(ai_tekst, "tekst_voor", side_effect=nep):
            run_cycle(campaign(), self.store, live=False, offline=True)
            eerste = len(aanroepen)
            self.assertGreater(eerste, 0)

            demo = self.store.one("SELECT versie, html FROM demos LIMIT 1")
            self.assertEqual(demo["versie"], f"{DEMO_VERSIE}+ai{ai_tekst.TEKST_VERSIE}")
            self.assertIn("Zelf gebakken, elke dag", demo["html"])

            # Ontwerp gewijzigd: de pagina's worden opnieuw gebouwd, maar de
            # tekst komt uit de cache en kost dus niets extra.
            self.store.execute("UPDATE demos SET versie = ?", ("oud",))
            self.store.commit()
            run_cycle(campaign(), self.store, live=False, offline=True)
            self.assertEqual(len(aanroepen), eerste, "tweede keer betaald voor dezelfde tekst")

    def test_a_failed_text_still_gives_a_page_and_is_retried(self):
        from leadmachine import ai_tekst
        from leadmachine.demo import DEMO_VERSIE

        with mock.patch.object(ai_tekst, "ingeschakeld", return_value=True), \
                mock.patch.object(ai_tekst, "tekst_voor", return_value=None):
            counters = run_cycle(campaign(), self.store, live=False, offline=True)

        self.assertGreater(counters["demos"], 0)
        versies = {r["versie"] for r in self.store.execute("SELECT versie FROM demos")}
        # Gemarkeerd als gewone versie, dus een volgende beurt probeert hij het
        # opnieuw in plaats van met de vaste tekst te blijven staan.
        self.assertEqual(versies, {DEMO_VERSIE})
        html = self.store.one("SELECT html FROM demos LIMIT 1")["html"]
        self.assertIn("Waar we voor klaarstaan", html)


class TestBeurtLog(unittest.TestCase):
    """Een rij met nullen zonder uitleg is geen informatie. Wat de machine
    meldde hoort bewaard te blijven, ook als niemand op dat moment keek."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = database.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_what_a_cycle_reported_is_kept(self):
        run_cycle(campaign(), self.store, live=False, offline=True)
        run = database.recent_runs(self.store, 1)[0]
        logs = database.runlogs(self.store, [int(run["id"])])
        self.assertIn(int(run["id"]), logs)
        self.assertIn("Bedrijven ophalen", logs[int(run["id"])])

    def test_a_failed_search_says_why(self):
        def stuk(*args, **kwargs):
            raise RuntimeError("Overpass gaf niets terug - overpass-api.de: te veel verzoeken (429)")

        with mock.patch("leadmachine.pipeline.discover", side_effect=stuk):
            run_cycle(campaign(), self.store, live=False, offline=True)

        run = database.recent_runs(self.store, 1)[0]
        log = database.runlogs(self.store, [int(run["id"])])[int(run["id"])]
        self.assertIn("429", log)
        self.assertIn("te veel verzoeken", log)

        # En de volgende beurt zegt tot wanneer het ophalen stilligt.
        with mock.patch("leadmachine.pipeline.discover", side_effect=stuk):
            run_cycle(campaign(), self.store, live=False, offline=True)
        tweede = database.recent_runs(self.store, 1)[0]
        log2 = database.runlogs(self.store, [int(tweede["id"])])[int(tweede["id"])]
        self.assertIn("op pauze tot", log2)

    def test_old_logs_are_cleaned_up(self):
        for i in range(1, 40):
            database.bewaar_runlog(self.store, i, [f"beurt {i}"], houden=10)
        self.store.commit()
        bewaard = self.store.execute("SELECT key FROM meta WHERE key LIKE 'runlog:%'")
        self.assertEqual(len(bewaard), 10)
        # De nieuwste zijn er nog.
        self.assertIn("runlog:39", {r["key"] for r in bewaard})


class TestKnopDoorbreektPauze(unittest.TestCase):
    """Na een storing bij Overpass ligt het ophalen twintig minuten stil. Dat is
    er voor de cron, niet voor jou: druk je zelf op de knop, dan wil je een
    antwoord en niet 'staat op pauze'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "t.db")
        self.token = "test-token"
        self.eerder = {k: os.environ.get(k) for k in ("DASHBOARD_PASSWORD", "LM_HOSTED")}
        os.environ["DASHBOARD_PASSWORD"] = "geheim"
        os.environ["LM_HOSTED"] = "1"
        handler = make_handler(campaign(), self.db_path, self.token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()
        for sleutel, waarde in self.eerder.items():
            if waarde is None:
                os.environ.pop(sleutel, None)
            else:
                os.environ[sleutel] = waarde

    def test_the_button_forces_a_search(self):
        from leadmachine import dashboard

        gezien = {}

        def nep_cycle(campaign_, store, **kwargs):
            gezien.update(kwargs)
            return {"discovered": 0, "audited": 0, "demos": 0, "queued": 0, "sent": 0, "failed": 0}

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.open(urllib.request.Request(self.base + "/login", data=b"wachtwoord=geheim"), timeout=5)

        with mock.patch.object(dashboard, "run_cycle", side_effect=nep_cycle):
            request = urllib.request.Request(self.base + "/api/run", method="POST")
            request.add_header("X-LM-Token", self.token)
            opener.open(request, timeout=5).read()

        self.assertTrue(gezien.get("force_discover"), "de knop hoort de pauze te negeren")

    def test_a_paused_cycle_names_the_local_time(self):
        """De database rekent in UTC. Een tijd in een zin hoort te kloppen met
        de klok op je telefoon, anders lijkt de pauze twee uur geleden voorbij."""
        from leadmachine.store import klok

        store = database.connect(self.db_path)
        einde = now() + timedelta(minutes=20)
        database.set_meta(store, "discover_pauze_tot", stamp(einde))
        store.commit()
        run_cycle(campaign(), store, live=False, offline=True)
        run = database.recent_runs(store, 1)[0]
        log = database.runlogs(store, [int(run["id"])])[int(run["id"])]
        store.close()

        self.assertIn(f"op pauze tot {klok(einde)}", log)
        self.assertNotIn(einde.strftime("%H:%M"), log.split("pauze tot ")[1][:6])


class TestVolledigsteLeadsEerst(unittest.TestCase):
    """Een pagina met openingstijden en een telefoonnummer overtuigt; een
    pagina met alleen een straatnaam werkt tegen je. Nu er per gemeente
    honderden bedrijven binnenkomen, mag de machine kieskeurig zijn."""

    def test_the_richest_lead_gets_its_demo_first(self):
        from leadmachine.demo import DEMO_VERSIE

        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            bedrijven = [
                ("Kaal", {}),
                ("Alleen straat", {"street": "Dorpsstraat"}),
                ("Straat en telefoon", {"street": "Dorpsstraat", "phone": "0162 111222"}),
                ("Compleet", {"street": "Dorpsstraat", "phone": "0162 111222",
                              "opening_hours": "Mo-Fr 09:00-17:00", "email": "info@example.com"}),
            ]
            for i, (naam, extra) in enumerate(bedrijven):
                lead_id, _ = database.upsert_lead(store, {
                    "osm_type": "node", "osm_id": f"v{i}", "name": naam,
                    "niche": "garage", "city": "Dongen", "source": "test", **extra,
                })
                database.save_audit(store, lead_id, {"score": 55, "segment": "hot",
                                                     "findings": [], "reachable": 0})
            store.commit()

            volgorde = [
                rij["name"] for rij in
                database.leads_needing_demo(store, limit=4, min_score=45, versie=DEMO_VERSIE)
            ]
            self.assertEqual(volgorde[0], "Compleet")
            self.assertEqual(volgorde[1], "Straat en telefoon")
            self.assertEqual(volgorde[-1], "Kaal")
            store.close()

    def test_a_page_without_hours_or_phone_does_not_say_call_us(self):
        from leadmachine.config import load_campaign
        from leadmachine.demo import build_demo

        camp = load_campaign(EXAMPLE_CONFIG)
        kaal = {"name": "Garage Kaal", "niche": "garage", "city": "Dongen",
                "osm_id": "1", "street": "Stevensweg", "raw": "{}"}
        _, html = build_demo(kaal, camp)
        self.assertIn("const heeftTelefoon = false", html)

        met_telefoon = {**kaal, "phone": "0162 111222"}
        _, html2 = build_demo(met_telefoon, camp)
        self.assertIn("const heeftTelefoon = true", html2)
