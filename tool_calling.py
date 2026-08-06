"""Vendor-neutral Flask tool registry for agent memory operations."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any
import threading
import re

from memory_summarizer import contextual_welcome
from analytics import is_greeting


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
                },
                "required": ["organization_id", "mobile_no", "query"],
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
]


class ToolRegistry:
    def __init__(self, memory_store, handoff_builder, analytics_repository=None) -> None:
        self.store = memory_store
        self.handoff_builder = handoff_builder
        self.analytics = analytics_repository

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
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
            items = self.store.search(
                organization_id=arguments["organization_id"],
                mobile_no=arguments["mobile_no"],
                query=arguments["query"],
                session_id=arguments.get("session_id"),
                limit=limit,
            )
            return {"items": items, "count": len(items)}
        if name == "get_handoff_context":
            self._require(arguments, "organization_id", "mobile_no")
            org_id = arguments["organization_id"]
            mobile = arguments["mobile_no"]
            if self.analytics:
                profile = self.analytics.get_profile(org_id, mobile, session_limit=5)
                
                # Automatically push latest session summary to Mem0 with infer=True (memory extraction) on escalation
                if profile.get("session_summaries"):
                    latest_sess = profile["session_summaries"][0]
                    latest_session_id = latest_sess["session_id"]
                    latest_summary = latest_sess["summary"]
                    
                    try:
                        existing = self.store.recent(organization_id=org_id, mobile_no=mobile)
                        already_summarized = any(
                            item.get("session_id") == latest_session_id and
                            (item.get("metadata", {}).get("memory_type") == "session_summary" or item.get("memory_type") == "session_summary")
                            for item in (existing if isinstance(existing, list) else existing.get("results", []))
                        )
                        if not already_summarized:
                            print(f"Session {latest_session_id} escalated. Pushing summary to Mem0 async...")
                            
                            def _push():
                                try:
                                    self.store.add(
                                        organization_id=org_id,
                                        session_id=latest_session_id,
                                        mobile_no=mobile,
                                        text=latest_summary,
                                        role="system",
                                        infer=True,
                                        metadata={"memory_type": "session_summary"}
                                    )
                                except Exception as exc:
                                    print(f"Warning: async Mem0 push failed for {latest_session_id}: {exc}")
                                    
                            threading.Thread(target=_push, daemon=True).start()
                    except Exception as e:
                        print(f"Warning: Failed to verify/push session summary to Mem0: {e}")
                
                status = "Resolved" if profile["status"] == "resolved" else "Unresolved"
                
                latest_issue = profile['current_issue'][:180]
                previous_session_str = f"Status: {status}; previous action: {profile['previous_action'][:145]}"
                
                session_summaries = profile.get("session_summaries", [])
                if len(session_summaries) > 1:
                    prev_sess = session_summaries[1]
                    date_str = prev_sess.get("started_at", "")[:10]
                    prev_summary = prev_sess.get("summary", "")
                    issue_match = re.search(r"issue:\s*(.*?)(?:\s+action:|\s*$)", prev_summary, re.I | re.DOTALL)
                    prev_issue = issue_match.group(1).strip() if issue_match else prev_summary[:100]
                    previous_session_str = f"Previous session ({date_str}): {prev_issue}"
                else:
                    previous_session_str = "No prior session on record."

                history_bullets = [
                    f"Latest issue: {latest_issue}",
                    previous_session_str,
                    f"Recommended next action: {profile['recommended_next_action'][:180]}",
                ]
                
                # Fetch Mem0 memories with a strict 1.5-second timeout and filter noise
                mem0_facts = []
                try:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(self.store.recent, organization_id=org_id, mobile_no=mobile)
                        memories = future.result(timeout=6.0)
                    seen = set()
                    for m in memories:
                        text = str(m.get("text") or m.get("memory") or "").strip()
                        if not text or is_greeting(text) or text in seen:
                            continue
                        seen.add(text)
                        mem0_facts.append(text)
                except TimeoutError:
                    print("Warning: Mem0 vector search timed out after 6.0s.")
                except Exception as e:
                    print(f"Warning: Failed to fetch Mem0 memories: {e}")
                
                return {
                    "organization_id": arguments["organization_id"],
                    "mobile_no": arguments["mobile_no"],
                    "history_summary": history_bullets,
                    "memory_count": profile["memory_count"],
                    "memories": mem0_facts,
                    "mem0_facts": mem0_facts,
                    "profile_summary": profile["profile_summary"],
                    "current_issue": profile["current_issue"],
                    "status": profile["status"],
                    "recent_contacts": profile["recent_contacts"],
                    "previous_action": profile["previous_action"],
                    "recommended_next_action": profile["recommended_next_action"],
                    "previous_session_count": profile["previous_session_count"],
                    "session_summaries": profile["session_summaries"],
                    "has_older_sessions": profile["has_older_sessions"],
                    "cache": profile["cache"],
                    "updated_at": profile.get("updated_at"),
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
        if name == "get_contextual_welcome":
            self._require(arguments, "organization_id", "mobile_no")
            org_id = arguments["organization_id"]
            mobile = arguments["mobile_no"]
            if self.analytics:
                profile = self.analytics.get_profile(org_id, mobile, session_limit=1)
                if profile.get("memory_count"):
                    short = profile["current_issue"].strip().rstrip(".!?")[:100]
                    return {
                        "organization_id": org_id, "mobile_no": mobile,
                        "welcome_message": f'Hello! I see your last query was regarding "{short}". '
                                            "Has this been resolved, or can I help you further today?",
                        "memory_count": profile["memory_count"], "source": "precomputed-profile",
                    }
            # fall back to raw Mem0 traversal only if analytics has nothing
            memories = []
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self.store.recent, organization_id=org_id, mobile_no=mobile)
                    memories = future.result(timeout=1.5)
            except Exception as e:
                print(f"Warning: Mem0 welcome query failed or timed out: {e}")
            if memories:
                return {
                    "organization_id": org_id, "mobile_no": mobile,
                    "welcome_message": contextual_welcome(memories),
                    "memory_count": len(memories), "source": "mem0-vector-store",
                }
            return {"organization_id": org_id, "mobile_no": mobile,
                    "welcome_message": "Hello! How can I help you today?",
                    "memory_count": 0, "source": "default"}
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
