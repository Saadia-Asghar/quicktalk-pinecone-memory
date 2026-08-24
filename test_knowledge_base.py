import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pinecone_memory
from analytics import AnalyticsRepository
from flask_app import create_app
from knowledge_base import KnowledgeRepository, KnowledgeService, _safe_tone_output
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
        missing = self.service.search("org-b", "Can installation happen on weekends?")
        self.assertEqual(missing["status"], "no_evidence")
        self.assertFalse(missing["grounded"])
        self.assertIn("apologize", missing["answer"].lower())
        self.assertEqual(missing["searched_sources"], ["active_approved_agent_articles"])

    def test_incomplete_or_ambiguous_agent_replies_are_not_reusable(self):
        examples = (
            (
                ["Its urgent much", "??"],
                ["Yes Sir what issues Exactly.", "we can change it in case of any blacklisting issue"],
            ),
            (
                ["I have to increase my package speed", "It is 20mb and I want 30mb. What will be the price?"],
                ["billing will assist you in morning"],
            ),
        )
        for index, (customer_messages, agent_messages) in enumerate(examples):
            scope = f"org-reject-{index}"
            session = self.repository.create_session(scope, "customer-1", "agent-1")
            for text in customer_messages:
                self.repository.add_message(scope, session["id"], "customer", text)
            for text in agent_messages:
                self.repository.add_message(scope, session["id"], "agent", text)
            result = self.service.close_and_index(scope, session["id"])
            self.assertFalse(result["reusable"])
            self.assertIsNone(result["article"])
            self.assertEqual(self.repository.list_articles(scope), [])

    def test_tone_profile_is_separate_from_knowledge_facts(self):
        scope = "org-tone"
        session = self.repository.create_session(scope, "customer-1", "agent-1")
        self.repository.add_message(scope, session["id"], "customer", "Can you help me?")
        self.repository.add_message(scope, session["id"], "agent", "Sure sir, please share the details.")
        profile = self.repository.tone_profile(scope)
        self.assertEqual(profile["organization_id"], scope)
        self.assertEqual(profile["sample_count"], 1)
        self.assertFalse(profile["facts_learned"])
        self.assertIn("style only", profile["safety_rule"])
        policy = self.repository.curation_policy(scope)
        self.assertIn("transfers, referrals", " ".join(policy["reject"]))
        self.assertIn("prices_and_fees", policy["controlled_facts"])
        updated = self.repository.set_curation_policy(scope, {
            "controlled_facts": {"consultation_fee": "require doctor/location and effective date"}
        })
        self.assertEqual(updated["controlled_facts"]["consultation_fee"],
                         "require doctor/location and effective date")

    def test_tone_output_never_leaks_reasoning_or_prompt(self):
        original = "Welcome back! Is your previous internet issue resolved?"
        leaked = (
            "<think>Here's a thinking process: Analyze User Input. **Task:** Rewrite RESPONSE "
            "using STYLE_GUIDANCE. **Constraints:** Return only the rewritten response."
        )
        self.assertEqual(_safe_tone_output(leaked, original), original)
        completed_thought = "<think>private reasoning</think>Sure, is your internet issue resolved?"
        self.assertEqual(
            _safe_tone_output(completed_thought, original),
            "Sure, is your internet issue resolved?",
        )

    def test_price_knowledge_requires_scope_currency_and_validity(self):
        rejected = self.repository.create_session("org-price-bad", "customer", "agent")
        self.repository.add_message("org-price-bad", rejected["id"], "customer", "What is the package price?")
        self.repository.add_message("org-price-bad", rejected["id"], "agent", "The package costs 2500.")
        self.assertIsNone(self.service.close_and_index("org-price-bad", rejected["id"])["article"])

        accepted = self.repository.create_session("org-price-good", "customer", "agent")
        self.repository.add_message("org-price-good", accepted["id"], "customer", "What is the 30 Mbps package price?")
        self.repository.add_message(
            "org-price-good", accepted["id"], "agent",
            "The current 30 Mbps package price is PKR 2,500, effective from August 2026.",
        )
        self.assertIsNotNone(self.service.close_and_index("org-price-good", accepted["id"])["article"])

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
        transcript = client.get(
            f"/api/agent-chats/{session_id}/messages?organization_id=org-a", headers=headers
        )
        self.assertEqual(transcript.status_code, 200)
        transcript_data = transcript.get_json()
        self.assertEqual(len(transcript_data["messages"]), 2)
        self.assertEqual(transcript_data["messages"][1]["sender_role"], "agent")
        self.assertEqual(transcript_data["generated_memory"]["source_session_id"], session_id)
        mismatch = client.get("/api/knowledge/articles?organization_id=org-b", headers=headers)
        self.assertEqual(mismatch.status_code, 400)
        tool_mismatch = client.post("/api/tools/search_approved_knowledge/invoke", headers=headers, json={
            "arguments": {"organization_id": "org-b", "query": "weekend installation"}
        })
        self.assertEqual(tool_mismatch.status_code, 400)

    def test_live_tool_returns_agent_answer_or_explicit_apology(self):
        analytics = AnalyticsRepository(Path(self.temp.name) / "live-tool.db")
        client = create_app(MemoryStore(), analytics).test_client()
        scope = "org-live"
        session = client.post("/api/agent-chats", json={
            "organization_id": scope, "customer_id": "customer-1", "agent_id": "agent-1",
        }).get_json()
        for role, text in (
            ("customer", "Is weekend installation available?"),
            ("agent", "Weekend installation is available after scheduling confirms serviceability."),
        ):
            client.post(f"/api/agent-chats/{session['id']}/messages", json={
                "organization_id": scope, "sender_role": role, "text": text,
            })
        client.post(f"/api/agent-chats/{session['id']}/close", json={"organization_id": scope})

        found = client.post("/api/tools/search_approved_knowledge/invoke", json={
            "arguments": {"organization_id": scope, "query": "Can installation happen on weekends?"}
        }).get_json()["result"]
        self.assertEqual(found["status"], "answer_found")
        self.assertTrue(found["grounded"])
        self.assertEqual(found["answer_source"], "approved_agent_knowledge")

        missing = client.post("/api/tools/search_approved_knowledge/invoke", json={
            "arguments": {"organization_id": scope, "query": "What is the lunar office policy?"}
        }).get_json()["result"]
        self.assertEqual(missing["status"], "no_evidence")
        self.assertFalse(missing["grounded"])
        self.assertIn("apologize", missing["answer"].lower())

    def test_resolver_searches_bot_then_agent_then_apologizes(self):
        self.repository.upsert_bot_article(
            "org-a", "How can I pay my invoice?", "Invoices can be paid from the billing portal.",
        )
        self._completed_chat("org-a")

        primary = self.service.resolve("org-a", "Where can I pay my invoice?")
        self.assertEqual(primary["answer_source"], "bot_knowledge_base")
        self.assertFalse(primary["fallback_used"])
        self.assertEqual(primary["searched_sources"], ["bot_knowledge_base"])
        self.assertTrue(primary["tone_applied"])
        self.assertFalse(primary["tone_profile"]["facts_learned"])

        fallback = self.service.resolve("org-a", "Can installation happen on weekends?")
        self.assertEqual(fallback["answer_source"], "approved_agent_knowledge")
        self.assertTrue(fallback["fallback_used"])
        self.assertEqual(fallback["searched_sources"], [
            "bot_knowledge_base", "active_approved_agent_articles",
        ])

        missing = self.service.resolve("org-a", "What is the lunar office policy?")
        self.assertFalse(missing["grounded"])
        self.assertTrue(missing["fallback_used"])
        self.assertIn("either our bot knowledge base", missing["answer"])

    def test_live_resolver_records_and_reports_missing_topics(self):
        analytics = AnalyticsRepository(Path(self.temp.name) / "gap-tool.db")
        client = create_app(MemoryStore(), analytics).test_client()
        scope = "org-gap"
        missing = client.post("/api/tools/resolve_support_answer/invoke", json={
            "arguments": {"organization_id": scope, "session_id": "gap-session",
                          "mobile_no": "+923330000001",
                          "query": "Do you support teleportation appointments?"}
        }).get_json()["result"]
        self.assertFalse(missing["grounded"])
        self.assertEqual(missing["knowledge_gap_event"]["organization_id"], scope)

        report = client.post("/api/tools/get_missing_knowledge_topics/invoke", json={
            "arguments": {"organization_id": scope, "days": 30}
        }).get_json()["result"]
        self.assertEqual(report["count"], 1)
        self.assertFalse(report["llm_used"])
        self.assertEqual(report["topics"][0]["count"], 1)

    def test_customer_context_tool_has_an_executable_handler(self):
        analytics = AnalyticsRepository(Path(self.temp.name) / "context-tool.db")
        client = create_app(MemoryStore(), analytics).test_client()
        response = client.post("/api/tools/get_customer_memory_context/invoke", json={
            "arguments": {"organization_id": "org-context", "mobile_no": "+923330000001"}
        })
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["result"]
        self.assertEqual(result["organization_id"], "org-context")
        self.assertEqual(result["memory_count"], 0)
        self.assertIn("source", result)


if __name__ == "__main__":
    unittest.main()
