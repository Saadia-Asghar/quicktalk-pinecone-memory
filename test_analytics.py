from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analytics import AnalyticsRepository, classify_category


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = AnalyticsRepository(Path(self.temp.name) / "analytics.db")
        self.scope_a = "tenant-a--clinic"
        self.scope_b = "tenant-b--telecom"
        self.repo.register_organization(
            scope=self.scope_a, tenant_id="tenant-a", organization_id="clinic",
            organization_name="Clinic", industry="healthcare",
        )
        self.repo.register_organization(
            scope=self.scope_b, tenant_id="tenant-b", organization_id="telecom",
            organization_name="Telecom", industry="telecom",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_industry_specific_categories(self):
        self.assertEqual(classify_category("asthma cough appointment", "healthcare"), "Respiratory")
        self.assertEqual(classify_category("fiber internet disconnects", "telecom"), "Connectivity")

    def test_dashboard_metrics_and_resolution(self):
        self._record("one", self.scope_a, "My prescription refill is urgent")
        self._record("two", self.scope_a, "The medication refill is completed")
        result = self.repo.dashboard(self.scope_a)
        self.assertEqual(result["kpis"]["unique_customers"], 1)
        self.assertEqual(result["kpis"]["sessions"], 1)
        self.assertEqual(result["kpis"]["resolution_rate"], 100.0)
        self.assertEqual(result["categories"][0]["name"], "Medication")

    def test_tenant_dashboard_isolation(self):
        self._record("clinic", self.scope_a, "I need a doctor appointment")
        self._record("telecom", self.scope_b, "My fiber internet is down")
        clinic = self.repo.dashboard(self.scope_a)
        telecom = self.repo.dashboard(self.scope_b)
        self.assertEqual(clinic["kpis"]["memory_events"], 1)
        self.assertEqual(telecom["kpis"]["memory_events"], 1)
        self.assertNotEqual(clinic["categories"][0]["name"], telecom["categories"][0]["name"])

    def test_unknown_organization_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown organization"):
            self.repo.dashboard("tenant-does-not-exist")

    def _record(self, memory_id: str, scope: str, text: str) -> None:
        self.repo.record_memory(
            {
                "id": memory_id, "organization_id": scope, "mobile_no": "+923331234567",
                "session_id": "session-1", "role": "customer", "text": text,
                "timestamp": "2026-08-03T00:00:00+00:00",
            }
        )


if __name__ == "__main__":
    unittest.main()
