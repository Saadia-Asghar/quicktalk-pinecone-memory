import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pinecone_memory
from flask_app import create_app
from pinecone_memory import MemoryStore


class FlaskMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file_patch = patch.object(pinecone_memory, "DATA_FILE", Path(self.temp.name) / "memory.json")
        self.file_patch.start()
        self.client = create_app(MemoryStore()).test_client()

    def tearDown(self):
        self.file_patch.stop()
        self.temp.cleanup()

    def test_memory_and_three_bullet_handoff_are_tenant_isolated(self):
        base = {"session_id": "session-1", "mobile_no": "+92 333 1234567", "role": "customer"}
        self.client.post("/api/memories", json={**base, "organization_id": "org-a", "text": "My bill is wrong, please help"})
        self.client.post("/api/memories", json={**base, "organization_id": "org-a", "text": "Thanks, it is resolved"})
        self.client.post("/api/memories", json={**base, "organization_id": "org-b", "text": "Secret other tenant message"})

        response = self.client.get("/api/inbox/context-card?organization_id=org-a&mobile_no=%2B923331234567")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload["history_summary"]), 3)
        self.assertNotIn("Secret", " ".join(payload["history_summary"]))
        self.assertEqual(payload["memory_count"], 2)

    def test_required_metadata_is_validated(self):
        response = self.client.post("/api/memories", json={"organization_id": "org-a"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_limit_returns_400(self):
        response = self.client.get("/api/memories?organization_id=org-a&mobile_no=923331234567&limit=nope")
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
