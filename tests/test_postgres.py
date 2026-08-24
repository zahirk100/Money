"""Dezelfde pijplijn, maar op Postgres in plaats van SQLite.

Dit is de test die zegt of het op Supabase gaat werken. Hij slaat zichzelf over
als er geen testdatabase is:

    LM_TEST_DATABASE_URL=postgresql://... python tests/test_postgres.py
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadmachine import db as database  # noqa: E402
from leadmachine.config import load_campaign  # noqa: E402
from leadmachine.dashboard import _leads, _lead_detail, _overview  # noqa: E402
from leadmachine.pipeline import run_cycle, send_due  # noqa: E402
from leadmachine.store import open_store, now, stamp  # noqa: E402

DB_URL = os.environ.get("LM_TEST_DATABASE_URL", "")
EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "campaign.example.yaml"


def campaign(**overrides):
    camp = load_campaign(EXAMPLE_CONFIG)
    camp._raw["autopilot"] = {**(camp._raw.get("autopilot") or {}), "source": "fixture", **overrides}
    return camp


@unittest.skipUnless(DB_URL, "geen LM_TEST_DATABASE_URL ingesteld")
class TestOnPostgres(unittest.TestCase):
    def setUp(self):
        self.store = open_store(DB_URL)
        # Schone lei per test.
        self.store.execute("TRUNCATE leads, audits, demos, outreach_log, runs, meta, suppression "
                           "RESTART IDENTITY CASCADE")
        self.store.commit()

    def tearDown(self):
        self.store.close()

    def test_it_is_really_postgres(self):
        self.assertEqual(self.store.dialect, "postgres")

    def test_full_cycle_runs(self):
        counters = run_cycle(campaign(), self.store, live=False, offline=True)
        self.assertGreater(counters["discovered"], 0)
        self.assertGreater(counters["audited"], 0)
        self.assertGreater(counters["demos"], 0)
        self.assertGreater(counters["queued"], 0)

    def test_upsert_does_not_duplicate(self):
        run_cycle(campaign(), self.store, live=False, offline=True)
        first = int(self.store.scalar("SELECT COUNT(*) AS n FROM leads"))
        run_cycle(campaign(), self.store, live=False, offline=True, force_discover=True)
        self.assertEqual(int(self.store.scalar("SELECT COUNT(*) AS n FROM leads")), first)

    def test_timestamps_compare_correctly(self):
        """De valkuil van de vorige ronde: een tijdstempel dat als tekst niet
        vergelijkt met de databaseklok, waardoor mails blijven hangen."""
        run_cycle(campaign(send_mode="review", review_hours=12), self.store, live=False, offline=True)
        self.assertEqual(database.due_outreach(self.store, 10), [])

        self.store.execute("UPDATE outreach_log SET send_after = ? WHERE status = 'wacht'",
                           (stamp(now() - timedelta(hours=1)),))
        self.store.commit()
        self.assertTrue(database.due_outreach(self.store, 10))

    def test_auto_mode_is_due_immediately(self):
        run_cycle(campaign(send_mode="auto"), self.store, live=False, offline=True)
        self.assertTrue(database.due_outreach(self.store, 10))

    def test_daily_counter_and_cooldown(self):
        run_cycle(campaign(send_mode="auto"), self.store, live=False, offline=True)
        item = database.due_outreach(self.store, 1)[0]
        database.mark_outreach(self.store, item["id"], "verstuurd")
        self.store.commit()
        self.assertEqual(database.sent_today(self.store), 1)
        laatst = database.last_contact(self.store, item["lead_id"])
        self.assertIsNotNone(laatst)
        self.assertLess(now() - laatst, timedelta(minutes=5))

    def test_demo_html_is_stored_and_findable_by_slug(self):
        run_cycle(campaign(), self.store, live=False, offline=True)
        row = self.store.one("SELECT slug FROM demos LIMIT 1")
        demo = database.demo_by_slug(self.store, row["slug"])
        self.assertIn("<!doctype html>", demo["html"].lower())

    def test_suppression_on_address_and_domain(self):
        database.suppress(self.store, "info@voorbeeld.example.com")
        database.suppress(self.store, "ander.example.org")
        self.store.commit()
        self.assertTrue(database.is_suppressed(self.store, "info@voorbeeld.example.com"))
        self.assertTrue(database.is_suppressed(self.store, "wie.dan.ook@ander.example.org"))
        self.assertFalse(database.is_suppressed(self.store, "hallo@nog.example.net"))

    def test_dashboard_queries_work(self):
        run_cycle(campaign(), self.store, live=False, offline=True)
        overzicht = _overview(self.store, campaign())
        self.assertEqual(len(overzicht["sent_per_day"]), 14)
        aantallen = [stap["aantal"] for stap in overzicht["funnel"]]
        self.assertEqual(aantallen, sorted(aantallen, reverse=True))
        self.assertEqual(overzicht["autopilot"]["opslag"], "postgres")

        leads = _leads(self.store, {"q": ["schaar"]})
        self.assertTrue(leads)
        detail = _lead_detail(self.store, leads[0]["id"])
        self.assertEqual(detail["id"], leads[0]["id"])

    def test_stats_survive_an_empty_database(self):
        from leadmachine.report import stats

        data = stats(self.store)
        self.assertEqual(data["leads"], 0)
        self.assertEqual(data["sent_today"], 0)

    def test_send_stops_at_the_daily_limit(self):
        camp = campaign(send_mode="auto")
        camp.outreach["daily_limit"] = 0
        run_cycle(camp, self.store, live=False, offline=True)
        self.assertEqual(send_due(camp, self.store, live=True), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
