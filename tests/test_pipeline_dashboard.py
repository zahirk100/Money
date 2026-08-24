"""Tests voor de cyclus, de wachtrij en het dashboard. Volledig offline:
de bedrijven komen uit de fixture en er wordt nooit echt gemaild.

    python tests/test_pipeline_dashboard.py
"""

from __future__ import annotations

import json
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

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "campaign.example.yaml"


def campaign(**overrides):
    camp = load_campaign(EXAMPLE_CONFIG)
    camp._raw.setdefault("autopilot", {})
    camp._raw["autopilot"] = {**(camp._raw["autopilot"] or {}), "source": "fixture", **overrides}
    return camp


class TestCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = database.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_cycle_fills_the_whole_pipeline(self):
        counters = run_cycle(campaign(), self.conn, live=False, offline=True)
        self.assertGreater(counters["discovered"], 0)
        self.assertEqual(counters["audited"], counters["discovered"])
        self.assertGreater(counters["demos"], 0)
        self.assertGreater(counters["queued"], 0)
        # Zonder live=True gaat er niets de deur uit.
        self.assertEqual(counters["sent"], 0)

    def test_cycle_is_recorded_as_a_run(self):
        run_cycle(campaign(), self.conn, trigger="test", live=False, offline=True)
        run = database.recent_runs(self.conn, 1)[0]
        self.assertEqual(run["trigger"], "test")
        self.assertEqual(run["status"], "klaar")
        self.assertIsNotNone(run["finished_at"])

    def test_second_cycle_does_not_queue_the_same_lead_twice(self):
        first = run_cycle(campaign(), self.conn, live=False, offline=True)
        second = run_cycle(campaign(), self.conn, live=False, offline=True)
        self.assertGreater(first["queued"], 0)
        self.assertEqual(second["queued"], 0)

    def test_discovery_is_skipped_when_recent(self):
        run_cycle(campaign(), self.conn, live=False, offline=True)
        second = run_cycle(campaign(), self.conn, live=False, offline=True)
        self.assertEqual(second["discovered"], 0)

    def test_review_mode_delays_sending(self):
        run_cycle(campaign(send_mode="review", review_hours=12), self.conn, live=False, offline=True)
        item = database.pending_outreach(self.conn)[0]
        send_after = datetime.fromisoformat(item["send_after"])
        self.assertGreater(send_after, datetime.utcnow() + timedelta(hours=11))
        # Nog niets aan de beurt.
        self.assertEqual(database.due_outreach(self.conn, 10), [])

    def test_auto_mode_queues_for_immediate_sending(self):
        run_cycle(campaign(send_mode="auto"), self.conn, live=False, offline=True)
        self.assertTrue(database.due_outreach(self.conn, 10))

    def test_min_score_keeps_weak_leads_out_of_the_queue(self):
        run_cycle(campaign(min_score=101), self.conn, live=False, offline=True)
        self.assertEqual(database.pending_outreach(self.conn), [])

    def test_daily_limit_stops_sending(self):
        camp = campaign(send_mode="auto")
        camp.outreach["daily_limit"] = 0
        run_cycle(camp, self.conn, live=False, offline=True)
        sent, failed = send_due(camp, self.conn, live=True)
        self.assertEqual((sent, failed), (0, 0))

    def test_suppressed_address_is_dropped_just_before_sending(self):
        camp = campaign(send_mode="auto")
        run_cycle(camp, self.conn, live=False, offline=True)
        item = database.due_outreach(self.conn, 1)[0]
        database.suppress(self.conn, item["to_addr"], "test")
        self.conn.commit()
        sent, _ = send_due(camp, self.conn, live=True, limit=1)
        self.assertEqual(sent, 0)
        row = self.conn.execute(
            "SELECT status FROM outreach_log WHERE id = ?", (item["id"],)
        ).fetchone()
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
            conn = database.connect(Path(tmp) / "t.db")
            run_cycle(campaign(), conn, live=False, offline=True)
            # Twee mails naar hetzelfde bedrijf mogen de trechter niet laten groeien.
            lead_id = conn.execute("SELECT lead_id FROM outreach_log LIMIT 1").fetchone()["lead_id"]
            for _ in range(3):
                database.log_outreach(conn, lead_id, status="verstuurd", to_addr="x@y.nl")
            conn.commit()
            funnel = _overview(conn, campaign())["funnel"]
            aantallen = [stap["aantal"] for stap in funnel]
            self.assertEqual(aantallen, sorted(aantallen, reverse=True))
            conn.close()


class TestDashboardServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "t.db")
        conn = database.connect(cls.db_path)
        run_cycle(campaign(), conn, live=False, offline=True)
        conn.close()

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

    def test_unknown_route_is_a_clean_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/bestaat-niet")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
