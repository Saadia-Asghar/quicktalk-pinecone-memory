import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pinecone_memory
from analytics import AnalyticsRepository
from custom_app import create_custom_app
from pinecone_memory import MemoryStore


class CustomAppTests(unittest.TestCase):
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
        self.client = create_custom_app(MemoryStore(), self.analytics).test_client()

    def tearDown(self):
        self.file_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def test_custom_page_and_full_handoff_flow(self):
        with self.client.get("/custom") as page:
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"Customer at a glance", page.data)

        identity = {
            "organization_id": "custom-test",
            "mobile_no": "+923331234567",
        }
        saved = self.client.post("/api/tools/save_customer_memory/invoke", json={
            "arguments": {
                **identity,
                "session_id": "custom-session",
                "role": "customer",
                "text": "My billing issue is still unresolved",
            }
        })
        self.assertEqual(saved.status_code, 200)

        handoff = self.client.post("/api/tools/get_handoff_context/invoke", json={
            "arguments": identity
        })
        result = handoff.get_json()["result"]
        self.assertEqual(len(result["history_summary"]), 3)
        self.assertTrue(
            "billing issue" in result["history_summary"][0] or
            "billing" in result["history_summary"][0].lower()
        )
        self.assertEqual(len(result["session_summaries"]), 1)
        self.assertEqual(result["session_summaries"][0]["session_id"], "custom-session")
        self.assertEqual(result["session_summaries"][0]["message_count"], 1)
        self.assertTrue(
            "billing issue" in result["session_summaries"][0]["summary"] or
            "billing" in result["session_summaries"][0]["summary"].lower()
        )


if __name__ == "__main__":
    unittest.main()
