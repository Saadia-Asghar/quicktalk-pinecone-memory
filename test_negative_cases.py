"""Negative and abuse-case tests for the Flask memory tool service."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pinecone_memory
from analytics import AnalyticsRepository
from flask_app import create_app
from pinecone_memory import MemoryStore


class NegativeMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict(
            "os.environ",
            {"PINECONE_API_KEY": "", "MEMORY_BACKEND": "pinecone", "SERVICE_API_KEY": ""},
            clear=False,
        )
        self.env_patch.start()
        self.file_patch = patch.object(
            pinecone_memory, "DATA_FILE", Path(self.temp.name) / "memory.json"
        )
        self.file_patch.start()
        self.analytics = AnalyticsRepository(Path(self.temp.name) / "analytics.db")
        self.client = create_app(MemoryStore(), self.analytics).test_client()

    def tearDown(self):
        self.file_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_missing_required_memory_fields_are_rejected(self):
        response = self.client.post("/api/memories", json={"organization_id": "org-a"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Missing required fields", response.get_json()["error"])

    def test_invalid_mobile_is_rejected(self):
        response = self._save(mobile_no="123")
        self.assertEqual(response.status_code, 400)
        self.assertIn("7 to 15 digits", response.get_json()["error"])

    def test_invalid_role_is_rejected(self):
        response = self._save(role="administrator")
        self.assertEqual(response.status_code, 400)
        self.assertIn("role must be", response.get_json()["error"])

    def test_timestamp_without_timezone_is_rejected(self):
        response = self._save(timestamp="2026-07-29T10:00:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("timezone", response.get_json()["error"])

    def test_malformed_json_is_rejected_as_json_error(self):
        response = self.client.post(
            "/api/memories", data="{broken", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsInstance(response.get_json(), dict)

    def test_unknown_tool_is_rejected(self):
        response = self.client.post("/api/tools/delete_everything/invoke", json={})
        self.assertEqual(response.status_code, 404)
        self.assertIn("Unknown tool", response.get_json()["error"])

    def test_non_object_tool_arguments_are_rejected(self):
        response = self.client.post(
            "/api/tools/save_customer_memory/invoke", json={"arguments": ["wrong"]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", response.get_json()["error"])

    def test_wrong_api_key_is_rejected(self):
        with patch.dict("os.environ", {"SERVICE_API_KEY": "correct-secret"}, clear=False):
            protected = create_app(MemoryStore(), self.analytics).test_client()
            response = protected.get("/api/tools", headers={"X-API-Key": "wrong-secret"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json()["error"], "unauthorized")

    def test_organization_isolation_prevents_cross_tenant_recall(self):
        self.assertEqual(self._save(organization_id="org-a").status_code, 201)
        leaked = self.client.get(
            "/api/memories?organization_id=org-b&mobile_no=%2B923331234567&q=billing"
        ).get_json()
        self.assertEqual(leaked["count"], 0)

    def test_mobile_isolation_prevents_cross_customer_recall(self):
        self.assertEqual(self._save(mobile_no="+923331234567").status_code, 201)
        leaked = self.client.get(
            "/api/memories?organization_id=org-a&mobile_no=%2B923339999999&q=billing"
        ).get_json()
        self.assertEqual(leaked["count"], 0)

    def test_session_filter_prevents_cross_session_recall(self):
        self.assertEqual(self._save(session_id="session-a").status_code, 201)
        result = self.client.get(
            "/api/memories?organization_id=org-a&mobile_no=%2B923331234567"
            "&session_id=session-b&q=billing"
        ).get_json()
        self.assertEqual(result["count"], 0)

    def test_empty_history_still_returns_exactly_three_safe_bullets(self):
        result = self.client.get(
            "/api/inbox/context-card?organization_id=empty-org&mobile_no=%2B923331234567"
        ).get_json()
        self.assertEqual(result["memory_count"], 0)
        self.assertEqual(len(result["history_summary"]), 3)

    def _save(self, **overrides):
        body = {
            "organization_id": "org-a",
            "session_id": "session-a",
            "mobile_no": "+923331234567",
            "text": "My billing issue is unresolved",
            "role": "customer",
        }
        body.update(overrides)
        return self.client.post("/api/memories", json=body)


if __name__ == "__main__":
    unittest.main()
