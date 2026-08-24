"""Vendor-neutral Flask tool registry for agent memory operations."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any
import threading
import re

from memory_summarizer import answer_from_memories, contextual_welcome
from analytics import is_greeting


def _requests_customer_history(query: str) -> bool:
    """Route only explicit personal-history questions to conversational memory answers."""
    return bool(re.search(
        r"\b(?:previous(?:ly)?|last\s+(?:time|week|month|session)|earlier|before|remember|remind|"
        r"discussed|follow[ -]?up|did\s+i|was\s+my|what\s+did\s+i|which\s+doctor\s+did|"
        r"that\s+(?:appointment|booking|case|ticket)|my\s+(?:previous|last|appointment|booking|"
        r"doctor|package|plan|case|ticket|request))\b", query, re.I,
    ))


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "save_customer_memory",
            "description": "Save one customer or assistant message as organizational memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "session_id": {"type": "string"},
                    "mobile_no": {"type": "string"},
                    "text": {"type": "string"},
                    "role": {"type": "string", "enum": ["customer", "assistant", "system"]},
                    "timestamp": {"type": "string", "description": "Optional ISO-8601 timestamp"},
                },
                "required": ["organization_id", "session_id", "mobile_no", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_customer_memory",
            "description": "Semantically search one customer's memories within one organization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "mobile_no": {"type": "string"},
                    "query": {"type": "string"},
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "generate_answer": {"type": "boolean", "description": "Generate a grounded LLM response from retrieved memories."},
                },
                "required": ["organization_id", "mobile_no", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_organization_tone_profile",
            "description": "Return organization-scoped style-only guidance learned from human-agent chats. It never returns chat facts as knowledge.",
            "parameters": {
                "type": "object",
                "properties": {"organization_id": {"type": "string"}},
                "required": ["organization_id"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_knowledge_curation_policy",
            "description": "Return the tenant-scoped rules controlling which human-agent answers may be saved as reusable Mem0 knowledge.",
            "parameters": {
                "type": "object",
                "properties": {"organization_id": {"type": "string"}},
                "required": ["organization_id"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_support_answer",
            "description": "Search bot knowledge first, then approved human-agent knowledge; apologize only if both miss.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "session_id": {"type": "string"},
                    "mobile_no": {"type": "string"},
                },
                "required": ["organization_id", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_missing_knowledge_topics",
            "description": "List tenant-scoped topics the bot could not answer after searching both knowledge sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 365},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["organization_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_approved_knowledge",
            "description": "Search the organization's active, auto-approved human-agent knowledge and generate a grounded answer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["organization_id", "query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_agent_history_from_json",
            "description": "Trigger a background process to import and train the bot on the 16,500+ agent chat histories from the specified JSON file. Returns immediately while processing continues in background.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contextual_welcome",
            "description": "Generate a real-time welcome from the customer's latest 30-day memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "mobile_no": {"type": "string"},
                },
                "required": ["organization_id", "mobile_no"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_handoff_context",
            "description": "Return three summary bullets plus every customer memory from the last 30 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "mobile_no": {"type": "string"},
                },
                "required": ["organization_id", "mobile_no"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_memory_context",
            "description": "Call this tool at the start of a new chat session to load the user's past memories and durable facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "organization_id": {"type": "string"},
                    "mobile_no": {"type": "string"},
                },
                "required": ["organization_id", "mobile_no"],
                "additionalProperties": False,
            },
        },
    },
]


class ToolRegistry:
    def __init__(self, memory_store, handoff_builder, analytics_repository=None, knowledge_service=None) -> None:
        self.store = memory_store
        self.handoff_builder = handoff_builder
        self.analytics = analytics_repository
        self.knowledge = knowledge_service

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
            
        if name == "import_agent_history_from_json":
            import subprocess
            import os
            script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "import_agent_history.py")
            subprocess.Popen(["python", script_path])
            return {"status": "success", "message": "Import script has been launched in the background. It will process 16,588 sessions and extract durable facts via Mem0."}

        if name == "save_customer_memory":
            self._require(arguments, "organization_id", "session_id", "mobile_no", "text")
            record = self.store.add(
                organization_id=arguments["organization_id"],
                session_id=arguments["session_id"],
                mobile_no=arguments["mobile_no"],
                text=arguments["text"],
                role=arguments.get("role", "customer"),
                timestamp=arguments.get("timestamp"),
                infer=False,
            )
            if self.analytics:
                self.analytics.record_memory(record)
            return record
        if name == "search_customer_memory":
            self._require(arguments, "organization_id", "mobile_no", "query")
            limit = self._limit(arguments.get("limit", 10))
            history_intent = _requests_customer_history(str(arguments["query"]))
            if not history_intent:
                return {"items": [], "count": 0, "retrieval": "skipped-general-question",
                        "history_intent": False, "answer_eligible": False, "grounded": False}
            items = self.store.search(
                organization_id=arguments["organization_id"],
                mobile_no=arguments["mobile_no"],
                query=arguments["query"],
                session_id=arguments.get("session_id"),
                limit=limit,
            )
            result = {"items": items, "count": len(items), "retrieval": "mem0-semantic",
                      "history_intent": history_intent, "answer_eligible": history_intent and bool(items)}
            if arguments.get("generate_answer") and result["answer_eligible"]:
                answer = answer_from_memories(str(arguments["query"]), items)
                if self.knowledge:
                    answer, tone = self.knowledge.apply_tone(str(arguments["organization_id"]), answer)
                    result["tone_profile"] = tone
                    result["tone_applied"] = True
                result["answer"] = answer
                result["grounded"] = bool(items)
            return result
        if name == "search_organization_memory":
            self._require(arguments, "organization_id", "query")
            limit = self._limit(arguments.get("limit", 15))
            items = self.store.search(
                organization_id=arguments["organization_id"],
                mobile_no=None,
                query=arguments["query"],
                limit=limit,
            )
            result = {"items": items, "count": len(items), "retrieval": "mem0-org-wide"}
            if arguments.get("generate_answer"):
                result["answer"] = answer_from_memories(str(arguments["query"]), items)
                result["grounded"] = bool(items)
            return result
        if name == "search_approved_knowledge":
            self._require(arguments, "organization_id", "query")
            if not self.knowledge:
                return {
                    "status": "no_evidence",
                    "answer": "I apologize, but approved human-agent knowledge is currently unavailable.",
                    "items": [], "grounded": False,
                    "answer_source": "approved_agent_knowledge",
                }
            return self.knowledge.search(
                str(arguments["organization_id"]), str(arguments["query"]),
                self._limit(arguments.get("limit", 5)),
            )
        if name == "get_organization_tone_profile":
            self._require(arguments, "organization_id")
            if not self.knowledge:
                raise ValueError("knowledge service is unavailable")
            return self.knowledge.repository.tone_profile(str(arguments["organization_id"]))
        if name == "get_knowledge_curation_policy":
            self._require(arguments, "organization_id")
            if not self.knowledge:
                raise ValueError("knowledge service is unavailable")
            return self.knowledge.repository.curation_policy(str(arguments["organization_id"]))
        if name == "resolve_support_answer":
            self._require(arguments, "organization_id", "query")
            if not self.knowledge:
                return {"status": "no_evidence", "grounded": False,
                        "answer": "I apologize, but the knowledge service is currently unavailable.",
                        "searched_sources": ["bot_knowledge_base", "active_approved_agent_articles"]}
            result = self.knowledge.resolve(
                str(arguments["organization_id"]), str(arguments["query"]),
                self._limit(arguments.get("limit", 5)),
            )
            if not result.get("grounded") and self.analytics:
                result["knowledge_gap_event"] = self.analytics.record_live_knowledge_gap(
                    str(arguments["organization_id"]), str(arguments["query"]),
                    arguments.get("session_id"), arguments.get("mobile_no"),
                )
            return result
        if name == "get_missing_knowledge_topics":
            self._require(arguments, "organization_id")
            if not self.analytics:
                return {"topics": [], "count": 0}
            topics = self.analytics.missing_knowledge_topics(
                str(arguments["organization_id"]), int(arguments.get("days", 30)),
                int(arguments.get("limit", 20)),
            )
            return {"topics": topics, "count": len(topics),
                    "source": "live_unanswered_queries", "llm_used": False}
        if name == "get_handoff_context":
            self._require(arguments, "organization_id", "mobile_no")
            org_id = arguments["organization_id"]
            mobile = arguments["mobile_no"]
            if self.analytics:
                profile = self.analytics.get_profile(org_id, mobile, session_limit=5)
                session_summaries = profile.get("session_summaries", [])
                summary_bullets = [item["summary"] for item in session_summaries[:3]]
                summary_bullets.extend(
                    ["No additional session summary is available."] * (3 - len(summary_bullets))
                )
                return {
                    "organization_id": arguments["organization_id"],
                    "mobile_no": arguments["mobile_no"],
                    "history_summary": summary_bullets,
                    "memory_count": profile["memory_count"],
                    "previous_session_count": profile["previous_session_count"],
                    "session_summaries": session_summaries,
                    "has_older_sessions": profile["has_older_sessions"],
                    "cache": profile["cache"],
                    "updated_at": profile.get("updated_at"),
                    "memory_role": "Mem0/Pinecone stores conversational history; summaries are generated separately per session.",
                }
            
            # Non-analytics fallback with timeout
            memories = []
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        self.store.recent,
                        organization_id=arguments["organization_id"],
                        mobile_no=arguments["mobile_no"]
                    )
                    memories = future.result(timeout=1.5)
            except Exception as e:
                print(f"Warning: Recent memory query failed: {e}")
            return {
                "organization_id": arguments["organization_id"],
                "mobile_no": arguments["mobile_no"],
                "history_summary": self.handoff_builder(memories),
                "memory_count": len(memories),
                "memories": memories,
                "profile_summary": "No precomputed profile repository is configured.",
                "session_summaries": [],
            }
        if name == "get_customer_memory_context":
            self._require(arguments, "organization_id", "mobile_no")
            organization_id = str(arguments["organization_id"])
            mobile_no = str(arguments["mobile_no"])
            if self.analytics:
                profile = self.analytics.get_profile(organization_id, mobile_no, session_limit=5)
                return {
                    "organization_id": organization_id,
                    "mobile_no": mobile_no,
                    "memory_count": profile.get("memory_count", 0),
                    "previous_session_count": profile.get("previous_session_count", 0),
                    "current_issue": profile.get("current_issue"),
                    "session_summaries": profile.get("session_summaries", []),
                    "source": "tenant-scoped-precomputed-profile-and-memory",
                }
            memories = self.store.recent(organization_id=organization_id, mobile_no=mobile_no)
            return {"organization_id": organization_id, "mobile_no": mobile_no,
                    "memory_count": len(memories), "memories": memories,
                    "source": "tenant-scoped-memory"}
        if name == "get_contextual_welcome":
            self._require(arguments, "organization_id", "mobile_no")
            org_id = arguments["organization_id"]
            mobile = arguments["mobile_no"]
            if self.analytics:
                profile = self.analytics.get_profile(org_id, mobile, session_limit=1)
                if profile.get("memory_count"):
                    summary_text = profile.get("session_summaries", [{}])[0].get("summary", "") if profile.get("session_summaries") else ""
                    if summary_text:
                        issue_match = re.search(r"issue:\s*(.*?)(?:\s+action:|\s+outcome:|\s*$)", summary_text, re.I | re.DOTALL)
                        short = issue_match.group(1).strip().rstrip(" .!?|")[:100] if issue_match else summary_text.strip().rstrip(" .!?|")[:100]
                    else:
                        short = profile.get("current_issue", "").strip().rstrip(" .!?|")[:100]
                    welcome = f'Welcome back! During our last session, we were discussing "{short}". '
                    welcome += "Has that been fully resolved, or do you need further assistance with it today?"
                    tone = None
                    if self.knowledge:
                        welcome, tone = self.knowledge.apply_tone(org_id, welcome)
                    return {
                        "organization_id": org_id, "mobile_no": mobile,
                        "welcome_message": welcome,
                        "memory_count": profile["memory_count"], "source": "precomputed-profile",
                        "tone_applied": bool(tone), "tone_profile": tone,
                    }
                welcome, tone = "Hello! How can I help you today?", None
                if self.knowledge:
                    welcome, tone = self.knowledge.apply_tone(org_id, welcome)
                return {"organization_id": org_id, "mobile_no": mobile,
                        "welcome_message": welcome, "memory_count": 0,
                        "source": "default-no-profile", "tone_applied": bool(tone),
                        "tone_profile": tone}
            # fall back to raw Mem0 traversal only if analytics has nothing
            memories = []
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.store.recent, organization_id=org_id, mobile_no=mobile)
                    memories = future.result(timeout=1.5)
            except Exception as e:
                print(f"Warning: Mem0 welcome query failed or timed out: {e}")
            if memories:
                welcome = contextual_welcome(memories)
                tone = None
                if self.knowledge:
                    welcome, tone = self.knowledge.apply_tone(org_id, welcome)
                return {
                    "organization_id": org_id, "mobile_no": mobile,
                    "welcome_message": welcome,
                    "memory_count": len(memories), "source": "mem0-vector-store",
                    "tone_applied": bool(tone), "tone_profile": tone,
                }
            welcome, tone = "Hello! How can I help you today?", None
            if self.knowledge:
                welcome, tone = self.knowledge.apply_tone(org_id, welcome)
            return {"organization_id": org_id, "mobile_no": mobile,
                    "welcome_message": welcome, "memory_count": 0, "source": "default",
                    "tone_applied": bool(tone), "tone_profile": tone}
        raise KeyError(name)

    @staticmethod
    def _require(arguments: dict[str, Any], *names: str) -> None:
        missing = [name for name in names if not arguments.get(name)]
        if missing:
            raise ValueError(f"Missing required arguments: {', '.join(missing)}")

    @staticmethod
    def _limit(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("limit must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        return min(max(parsed, 1), 50)
