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

    def test_mobile_identity_is_canonical(self):
        first = pinecone_memory.normalize_mobile("92 333 1234567")
        second = pinecone_memory.normalize_mobile("+92-333-1234567")
        self.assertEqual(first, second)

    def test_tool_registry_and_end_to_end_invocation(self):
        listed = self.client.get("/api/tools")
        self.assertEqual(listed.status_code, 200)
        names = {tool["function"]["name"] for tool in listed.get_json()["tools"]}
        self.assertEqual(names, {
            "save_customer_memory", "search_customer_memory", "get_handoff_context"
        })

        arguments = {
            "organization_id": "org-tools", "session_id": "session-tools",
            "mobile_no": "923331112222", "text": "My internet issue is still happening",
        }
        saved = self.client.post("/api/tools/save_customer_memory/invoke", json={"arguments": arguments})
        self.assertEqual(saved.status_code, 200)

        handoff = self.client.post("/api/tools/get_handoff_context/invoke", json={
            "arguments": {"organization_id": "org-tools", "mobile_no": "+923331112222"}
        })
        summary = handoff.get_json()["result"]["history_summary"]
        self.assertEqual(len(summary), 3)
        self.assertIn("internet issue", summary[0])

    def test_unknown_tool_is_json_404(self):
        response = self.client.post("/api/tools/not-a-tool/invoke", json={})
        self.assertEqual(response.status_code, 404)
        self.assertIn("Unknown tool", response.get_json()["error"])

    def test_role_and_timestamp_are_validated(self):
        body = {
            "organization_id": "org-a", "session_id": "session-a",
            "mobile_no": "+923331234567", "text": "hello", "role": "hacker",
        }
        self.assertEqual(self.client.post("/api/memories", json=body).status_code, 400)
        body["role"] = "customer"
        body["timestamp"] = "not-a-date"
        self.assertEqual(self.client.post("/api/memories", json=body).status_code, 400)

    @patch.dict("os.environ", {"SERVICE_API_KEY": "test-secret"}, clear=False)
    def test_api_key_protects_memory_and_tool_routes(self):
        protected = create_app(MemoryStore()).test_client()
        self.assertEqual(protected.get("/api/tools").status_code, 401)
        allowed = protected.get("/api/tools", headers={"X-API-Key": "test-secret"})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(protected.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
