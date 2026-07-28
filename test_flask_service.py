import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pinecone_memory
from mem0_memory import Mem0MemoryStore, create_memory_store
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

    def test_mem0_customer_identity_is_organization_scoped(self):
        first = Mem0MemoryStore._customer_id("org-a", "+92 333 1234567")
        same = Mem0MemoryStore._customer_id("org-a", "+923331234567")
        other_org = Mem0MemoryStore._customer_id("org-b", "+923331234567")
        self.assertEqual(first, same)
        self.assertNotEqual(first, other_org)

    @patch.dict("os.environ", {"MEMORY_BACKEND": "pinecone"}, clear=False)
    def test_direct_backend_remains_default_option(self):
        self.assertIsInstance(create_memory_store(), MemoryStore)

    @patch.dict("os.environ", {
        "PINECONE_API_KEY": "test-pinecone",
        "OPENAI_API_KEY": "test-openai",
        "MEM0_PINECONE_INDEX": "test-mem0",
    }, clear=False)
    def test_mem0_configures_an_organization_namespace(self):
        captured = {}

        class FakeMemory:
            @classmethod
            def from_config(cls, config):
                captured.update(config)
                return cls()

        with patch.dict("sys.modules", {"mem0": types.SimpleNamespace(Memory=FakeMemory)}):
            store = Mem0MemoryStore()
            store._client("org-a")

        vector = captured["vector_store"]["config"]
        self.assertEqual(vector["namespace"], "org-org-a")
        self.assertEqual(vector["collection_name"], "test-mem0")


if __name__ == "__main__":
    unittest.main()
