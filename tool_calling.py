"""Vendor-neutral Flask tool registry for agent memory operations."""

from __future__ import annotations

from typing import Any


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
            "name": "get_handoff_context",
            "description": "Return exactly three history bullets for the Human Agent Inbox.",
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
    def __init__(self, memory_store, handoff_builder) -> None:
        self.store = memory_store
        self.handoff_builder = handoff_builder

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        if name == "save_customer_memory":
            self._require(arguments, "organization_id", "session_id", "mobile_no", "text")
            return self.store.add(
                organization_id=arguments["organization_id"],
                session_id=arguments["session_id"],
                mobile_no=arguments["mobile_no"],
                text=arguments["text"],
                role=arguments.get("role", "customer"),
                timestamp=arguments.get("timestamp"),
            )
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
            memories = self.store.recent(
                organization_id=arguments["organization_id"], mobile_no=arguments["mobile_no"]
            )
            return {
                "organization_id": arguments["organization_id"],
                "mobile_no": arguments["mobile_no"],
                "history_summary": self.handoff_builder(memories),
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
