"""Tests draaien volledig offline: geen netwerk, geen echte database.

    python -m pytest tests -q      (of: python tests/test_leadmachine.py)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leadmachine import db as database  # noqa: E402
from leadmachine.audit import audit_lead, top_pitches  # noqa: E402
from leadmachine.config import load_campaign  # noqa: E402
from leadmachine.demo import parse_hours, render_demo, slugify  # noqa: E402
from leadmachine.discover import build_query, element_to_lead  # noqa: E402
from leadmachine.outreach import draft_email, eligible, wrap_paragraphs  # noqa: E402

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "campaign.example.yaml"


def campaign():
    return load_campaign(EXAMPLE_CONFIG)


class TestDiscover(unittest.TestCase):
    def test_query_contains_area_and_filters(self):
        query = build_query(campaign(), campaign().niches[0])
        self.assertIn('area["name"="Zwolle"]', query)
        self.assertIn('nwr["shop"="hairdresser"]', query)

    def test_element_without_name_is_skipped(self):
        self.assertIsNone(element_to_lead({"type": "node", "id": 1, "tags": {}}, "kapper", "test"))

    def test_tags_are_normalised(self):
        lead = element_to_lead(
            {
                "type": "way", "id": 42, "center": {"lat": 52.5, "lon": 6.1},
                "tags": {
                    "name": "Kapper X", "contact:website": "kapperx.example.com",
                    "contact:email": "mailto:info@kapperx.example.com",
                    "contact:phone": "+31 6 00000000", "addr:city": "Zwolle",
                },
            },
            "kapper", "test",
        )
        self.assertEqual(lead["website"], "https://kapperx.example.com")
        self.assertEqual(lead["email"], "info@kapperx.example.com")
        self.assertEqual(lead["lat"], 52.5)


class TestAudit(unittest.TestCase):
    def test_no_website_scores_hot(self):
        result = audit_lead({"name": "Kapper X", "website": None}, offline=True)
        self.assertGreaterEqual(result["score"], campaign().hot_threshold)
        self.assertEqual(campaign().segment(result["score"]), "hot")

    def test_social_only_is_flagged(self):
        result = audit_lead(
            {"name": "X", "website": "https://facebook.com/x"}, offline=True
        )
        self.assertIn("alleen_social", [f["code"] for f in result["findings"]])

    def test_http_site_flags_https(self):
        result = audit_lead({"name": "X", "website": "http://x.example.com"}, offline=True)
        self.assertIn("geen_https", [f["code"] for f in result["findings"]])

    def test_score_is_capped(self):
        findings = [{"code": "a", "weight": 80, "pitch": ""}, {"code": "b", "weight": 80, "pitch": ""}]
        from leadmachine.audit import _total

        self.assertEqual(_total(findings), 100)

    def test_pitches_are_ordered_by_weight(self):
        findings = [
            {"code": "a", "weight": 5, "pitch": "klein"},
            {"code": "b", "weight": 40, "pitch": "groot"},
        ]
        self.assertEqual(top_pitches(findings, 1), ["groot"])


class TestDemo(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slugify("Kapsalon Anné & Zn"), "kapsalon-anne-zn")

    def test_hours_are_translated(self):
        rows = parse_hours("Mo-Fr 08:00-17:00; Sa 09:00-13:00")
        self.assertEqual(rows[0]["days"], "Maandag-Vrijdag")
        self.assertEqual(rows[1]["time"], "09:00-13:00")

    def test_render_writes_page_with_business_details(self):
        lead = {
            "name": "Kapsalon De Schaar", "niche": "kapper", "city": "Zwolle",
            "street": "Voorbeeldstraat", "housenumber": "12", "postcode": "8011 AA",
            "phone": "+31 6 00000001", "email": None, "opening_hours": "Di-Vr 09:00-17:30",
            "lat": 52.5, "lon": 6.1, "osm_id": "1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            page = render_demo(lead, campaign(), out_dir=Path(tmp))
            html = page.read_text(encoding="utf-8")
        self.assertIn("Kapsalon De Schaar", html)
        self.assertIn("Dinsdag-Vrijdag", html)
        self.assertIn('href="tel:+31600000001"', html)
        self.assertNotIn("None", html)
        # De pagina moet zichzelf als voorbeeld aankondigen.
        self.assertIn("geen offici", html)

    def test_render_escapes_business_name(self):
        lead = {"name": "<script>x</script> BV", "niche": "kapper", "osm_id": "2"}
        with tempfile.TemporaryDirectory() as tmp:
            html = render_demo(lead, campaign(), out_dir=Path(tmp)).read_text()
        self.assertNotIn("<script>x</script>", html)


class TestOutreach(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = database.connect(Path(self.tmp.name) / "test.db")
        self.lead_id, _ = database.upsert_lead(
            self.store,
            {
                "osm_type": "node", "osm_id": "1", "name": "Kapsalon De Schaar",
                "niche": "kapper", "city": "Zwolle", "email": "info@voorbeeld.example.com",
                "phone": "+31 6 00000001",
            },
        )
        database.save_audit(
            self.store, self.lead_id,
            audit_lead({"name": "Kapsalon De Schaar", "website": None}, offline=True)
            | {"segment": "hot"},
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _row(self):
        return database.ranked_leads(self.store)[0]

    def test_draft_contains_offer_optout_and_sender(self):
        message = draft_email(self._row(), campaign(), demo_url="https://demo.example.com/x/")
        self.assertIn("Kapsalon De Schaar", message["subject"])
        self.assertIn("https://demo.example.com/x/", message["body"])
        self.assertIn("Liever geen mail meer", message["body"])
        self.assertIn(campaign().outreach["company_address"], message["body"])

    def test_draft_does_not_repeat_the_same_finding_twice(self):
        body = draft_email(self._row(), campaign())["body"]
        self.assertEqual(body.count("er is online geen eigen website te vinden"), 1)

    def test_suppressed_lead_is_skipped(self):
        database.suppress(self.store, "info@voorbeeld.example.com")
        ok, reason = eligible(self.store, self._row(), campaign())
        self.assertFalse(ok)
        self.assertIn("afmeldlijst", reason)

    def test_domain_level_suppression(self):
        database.suppress(self.store, "voorbeeld.example.com")
        ok, _ = eligible(self.store, self._row(), campaign())
        self.assertFalse(ok)

    def test_cooldown_blocks_second_mail(self):
        ok, _ = eligible(self.store, self._row(), campaign())
        self.assertTrue(ok)
        database.log_outreach(self.store, self.lead_id, status="verstuurd", to_addr="x@y.nl")
        ok, reason = eligible(self.store, self._row(), campaign())
        self.assertFalse(ok)
        self.assertIn("cooldown", reason)

    def test_lead_without_email_is_not_eligible(self):
        database.upsert_lead(
            self.store,
            {"osm_type": "node", "osm_id": "2", "name": "Zonder Mail", "niche": "kapper"},
        )
        row = self.store.one("SELECT * FROM leads WHERE osm_id = '2'")
        ok, reason = eligible(self.store, row, campaign())
        self.assertFalse(ok)
        self.assertIn("mailadres", reason)

    def test_wrapping_keeps_links_intact(self):
        text = "Een hele lange zin die zeker over de tweeenzeventig tekens heen gaat en dus afgebroken wordt.\n\nhttps://voorbeeld.example.com/een/heel/lang/pad/dat/niet/afgebroken/mag/worden/"
        wrapped = wrap_paragraphs(text)
        self.assertIn("https://voorbeeld.example.com/een/heel/lang/pad/dat/niet/afgebroken/mag/worden/", wrapped)
        self.assertTrue(all(len(line) <= 72 for line in wrapped.splitlines() if not line.startswith("http")))


class TestVerbindingsinstelling(unittest.TestCase):
    """De meest gemaakte fouten bij het instellen horen zichzelf uit te leggen."""

    def setUp(self):
        self._oud = {k: os.environ.get(k) for k in ("DATABASE_URL", "LM_HOSTED")}

    def tearDown(self):
        for sleutel, waarde in self._oud.items():
            if waarde is None:
                os.environ.pop(sleutel, None)
            else:
                os.environ[sleutel] = waarde

    def _fout(self, url, hosted=True):
        from leadmachine.store import OpslagOntbreekt, resolve_target

        os.environ["DATABASE_URL"] = url
        if hosted:
            os.environ["LM_HOSTED"] = "1"
        else:
            os.environ.pop("LM_HOSTED", None)
        with self.assertRaises(OpslagOntbreekt) as ctx:
            resolve_target()
        return str(ctx.exception)

    def test_api_url_instead_of_connection_string(self):
        melding = self._fout("https://abcdef.supabase.co", hosted=False)
        self.assertIn("Session pooler", melding)
        self.assertIn("postgresql://", melding)

    def test_placeholder_password(self):
        melding = self._fout(
            "postgresql://postgres.abc:[YOUR-PASSWORD]@aws-1-eu-central-1.pooler.supabase.com:5432/postgres",
            hosted=False,
        )
        self.assertIn("YOUR-PASSWORD", melding)

    def test_file_path_while_hosted(self):
        self.assertIn("postgresql://", self._fout("data/leads.db"))

    def test_a_proper_connection_string_passes(self):
        from leadmachine.store import resolve_target

        os.environ["DATABASE_URL"] = "postgresql://postgres:x@ergens.example.com:5432/postgres"
        os.environ["LM_HOSTED"] = "1"
        self.assertTrue(resolve_target().startswith("postgresql://"))


class TestDatabase(unittest.TestCase):
    def test_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            lead = {"osm_type": "node", "osm_id": "7", "name": "X", "niche": "kapper"}
            first, new1 = database.upsert_lead(store, lead)
            second, new2 = database.upsert_lead(store, {**lead, "name": "X BV"})
            self.assertEqual(first, second)
            self.assertTrue(new1)
            self.assertFalse(new2)
            row = store.one("SELECT name FROM leads WHERE id = ?", (first,))
            self.assertEqual(row["name"], "X BV")
            store.close()

    def test_bulk_upsert_counts_new_and_keeps_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            leads = [
                {"osm_type": "node", "osm_id": str(i), "name": f"Bedrijf {i}", "niche": "kapper"}
                for i in range(5)
            ]
            self.assertEqual(database.upsert_many(store, leads), 5)
            eerste = store.one("SELECT first_seen FROM leads WHERE osm_id = '1'")["first_seen"]

            leads[0]["name"] = "Bedrijf nul, hernoemd"
            leads.append({"osm_type": "node", "osm_id": "99", "name": "Nieuw", "niche": "kapper"})
            self.assertEqual(database.upsert_many(store, leads), 1)

            self.assertEqual(store.scalar("SELECT COUNT(*) AS n FROM leads"), 6)
            self.assertEqual(
                store.one("SELECT name FROM leads WHERE osm_id = '0'")["name"],
                "Bedrijf nul, hernoemd",
            )
            self.assertEqual(
                store.one("SELECT first_seen FROM leads WHERE osm_id = '1'")["first_seen"], eerste
            )
            self.assertEqual(database.upsert_many(store, []), 0)
            store.close()

    def test_audit_upsert_keeps_one_row_per_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = database.connect(Path(tmp) / "t.db")
            lead_id, _ = database.upsert_lead(
                store, {"osm_type": "node", "osm_id": "8", "name": "Y", "niche": "kapper"}
            )
            database.save_audit(store, lead_id, {"score": 10, "segment": "cold", "findings": []})
            database.save_audit(store, lead_id, {"score": 55, "segment": "hot", "findings": [{"code": "x"}]})
            rows = store.execute("SELECT * FROM audits WHERE lead_id = ?", (lead_id,))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["score"], 55)
            self.assertEqual(json.loads(rows[0]["findings"])[0]["code"], "x")
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
