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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadmachine import db as database  # noqa: E402
from leadmachine.config import load_campaign  # noqa: E402
from leadmachine.dashboard import _overview, make_handler  # noqa: E402
from leadmachine.pipeline import autopilot_settings, run_cycle, send_due  # noqa: E402
from leadmachine.store import now  # noqa: E402  # noqa: E402

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "campaign.example.yaml"


def campaign(**overrides):
    camp = load_campaign(EXAMPLE_CONFIG)
    camp._raw.setdefault("autopilot", {})
    camp._raw["autopilot"] = {**(camp._raw["autopilot"] or {}), "source": "fixture", **overrides}
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
        run_cycle(campaign(), self.store, live=False, offline=True)
        second = run_cycle(campaign(), self.store, live=False, offline=True)
        self.assertEqual(second["discovered"], 0)

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

    def test_unknown_route_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/bestaat-niet")
        self.assertEqual(ctx.exception.code, 404)


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
