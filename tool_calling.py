"""Vendor-neutral Flask tool registry for agent memory operations."""

from __future__ import annotations

from typing import Any

from memory_summarizer import contextual_welcome


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
            if self.analytics:
                profile = self.analytics.get_profile(
                    arguments["organization_id"], arguments["mobile_no"], session_limit=5
                )
                status = "Resolved" if profile["status"] == "resolved" else "Unresolved"
                return {
                    "organization_id": arguments["organization_id"],
                    "mobile_no": arguments["mobile_no"],
                    "history_summary": [
                        f"Current issue: {profile['current_issue'][:180]}",
                        f"Status: {status}; previous action: {profile['previous_action'][:145]}",
                        f"Recommended next action: {profile['recommended_next_action'][:180]}",
                    ],
                    "memory_count": profile["memory_count"],
                    "memories": [],
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
            memories = self.store.recent(
                organization_id=arguments["organization_id"], mobile_no=arguments["mobile_no"]
            )
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
            if self.analytics:
                profile = self.analytics.get_profile(
                    arguments["organization_id"], arguments["mobile_no"], session_limit=1
                )
                if profile["memory_count"]:
                    return {
                        "organization_id": arguments["organization_id"],
                        "mobile_no": arguments["mobile_no"],
                        "welcome_message": (
                            f"Hello! Is your previous issue—{profile['current_issue']}—resolved, "
                            "or would you like more help today?"
                        ),
                        "memory_count": profile["memory_count"],
                        "source": "precomputed-profile",
                    }
            memories = self.store.recent(
                organization_id=arguments["organization_id"], mobile_no=arguments["mobile_no"]
            )
            return {
                "organization_id": arguments["organization_id"],
                "mobile_no": arguments["mobile_no"],
                "welcome_message": contextual_welcome(memories),
                "memory_count": len(memories),
            }
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
