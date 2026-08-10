import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pinecone_memory
from analytics import AnalyticsRepository
from flask_app import create_app
from knowledge_base import KnowledgeRepository, KnowledgeService
from pinecone_memory import MemoryStore


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict("os.environ", {"SERVICE_API_KEY": "", "PINECONE_API_KEY": ""}, clear=False)
        self.env_patch.start()
        self.data_patch = patch.object(pinecone_memory, "DATA_FILE", Path(self.temp.name) / "memory.json")
        self.data_patch.start()
        self.llm_patch = patch("knowledge_base._ollama", return_value=None)
        self.llm_patch.start()
        self.repository = KnowledgeRepository(Path(self.temp.name) / "knowledge.db")
        self.service = KnowledgeService(self.repository, MemoryStore())

    def tearDown(self):
        self.llm_patch.stop()
        self.data_patch.stop()
        self.env_patch.stop()
        self.temp.cleanup()

    def _completed_chat(self, scope="org-a"):
        session = self.repository.create_session(scope, "customer-1", "verified-agent-1")
        self.repository.add_message(scope, session["id"], "customer", "Do you offer weekend installation appointments?")
        self.repository.add_message(scope, session["id"], "agent", "Weekend installation is available in selected areas and requires scheduling confirmation.")
        return self.service.close_and_index(scope, session["id"])

    def test_closed_agent_chat_is_auto_approved_and_tenant_isolated(self):
        result = self._completed_chat()
        article = result["article"]
        self.assertEqual(article["status"], "active")
        self.assertEqual(article["active_version"], 1)
        self.assertEqual(article["approved_by"], "application:auto-agent-chat")
        self.assertEqual(self.service.search("org-a", "Can installation happen on weekends?")["status"], "answer_found")
        self.assertEqual(self.service.search("org-b", "Can installation happen on weekends?")["status"], "no_evidence")

    def test_edit_auto_activates_new_version_and_disable_blocks_retrieval(self):
        article = self._completed_chat()["article"]
        edited = self.service.edit_and_index(
            "org-a", article["id"], article["canonical_question"],
            "Weekend installation is available only after scheduling confirms the area.", "admin-1",
        )
        self.assertEqual(edited["active_version"], 2)
        with self.repository._connect() as db:
            statuses = [row[0] for row in db.execute(
                "SELECT status FROM knowledge_article_versions WHERE article_id=? ORDER BY version", (article["id"],)
            )]
        self.assertEqual(statuses, ["superseded", "active"])
        self.repository.set_status("org-a", article["id"], "disabled", "admin-1")
        self.assertEqual(self.service.search("org-a", "weekend installation")["status"], "no_evidence")

    def test_flask_agent_portal_flow_and_scope_mismatch(self):
        analytics = AnalyticsRepository(Path(self.temp.name) / "app.db")
        client = create_app(MemoryStore(), analytics).test_client()
        headers = {"X-Organization-Scope": "org-a", "X-User-Role": "organization_admin"}
        created = client.post("/api/agent-chats", headers=headers, json={
            "organization_id": "org-a", "customer_id": "customer-1", "agent_id": "agent-1",
        })
        self.assertEqual(created.status_code, 201)
        session_id = created.get_json()["id"]
        for role, text in (("customer", "Is weekend installation available?"), ("agent", "Yes, subject to scheduling confirmation.")):
            response = client.post(f"/api/agent-chats/{session_id}/messages", headers=headers, json={
                "organization_id": "org-a", "sender_role": role, "text": text,
            })
            self.assertEqual(response.status_code, 201)
        closed = client.post(f"/api/agent-chats/{session_id}/close", headers=headers, json={"organization_id": "org-a"})
        self.assertEqual(closed.status_code, 200)
        mismatch = client.get("/api/knowledge/articles?organization_id=org-b", headers=headers)
        self.assertEqual(mismatch.status_code, 400)
        tool_mismatch = client.post("/api/tools/search_approved_knowledge/invoke", headers=headers, json={
            "arguments": {"organization_id": "org-b", "query": "weekend installation"}
        })
        self.assertEqual(tool_mismatch.status_code, 400)


if __name__ == "__main__":
    unittest.main()
