"""Pinecone-backed conversation memory with a development-safe local fallback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_FILE = Path(__file__).parent / "data" / "pinecone_fallback.json"
INDEX_NAME = os.getenv("PINECONE_INDEX", "quicktalk-memories")
DIMENSION = int(os.getenv("PINECONE_DIMENSION", "384"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_mobile(value: str) -> str:
    value = value.strip()
    prefix = "+" if value.startswith("+") else ""
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7 or len(digits) > 15:
        raise ValueError("mobile_no must contain 7 to 15 digits")
    return prefix + digits


def _namespace(organization_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", organization_id.strip())
    if not safe:
        raise ValueError("organization_id is required")
    return f"org-{safe[:48]}"


def _embedding(text: str) -> list[float]:
    """Deterministic feature hashing; replace with a hosted embedding model in production."""
    vector = [0.0] * DIMENSION
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSION
        vector[index] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class MemoryStore:
    def __init__(self) -> None:
        self._index = None
        api_key = os.getenv("PINECONE_API_KEY")
        if api_key:
            from pinecone import Pinecone

            self._index = Pinecone(api_key=api_key).Index(INDEX_NAME)

    @property
    def backend(self) -> str:
        return "pinecone" if self._index else "local-fallback"

    def add(self, *, organization_id: str, session_id: str, mobile_no: str,
            text: str, role: str = "customer", timestamp: str | None = None) -> dict[str, Any]:
        if not session_id.strip() or not text.strip():
            raise ValueError("session_id and text are required")
        mobile = normalize_mobile(mobile_no)
        record = {
            "id": str(uuid.uuid4()),
            "organization_id": organization_id.strip(),
            "session_id": session_id.strip(),
            "mobile_no": mobile,
            "timestamp": timestamp or utc_now(),
            "role": role,
            "text": text.strip(),
        }
        namespace = _namespace(organization_id)
        vector = _embedding(record["text"])
        if self._index:
            self._index.upsert(
                vectors=[{"id": record["id"], "values": vector, "metadata": record}],
                namespace=namespace,
            )
        else:
            rows = self._read_local()
            rows.append({**record, "namespace": namespace, "values": vector})
            self._write_local(rows)
        return {k: v for k, v in record.items() if k != "organization_id"} | {"organization_id": record["organization_id"]}

    def search(self, *, organization_id: str, mobile_no: str, query: str = "conversation history",
               session_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        mobile = normalize_mobile(mobile_no)
        namespace = _namespace(organization_id)
        filters: dict[str, Any] = {"mobile_no": {"$eq": mobile}}
        if session_id:
            filters["session_id"] = {"$eq": session_id}
        if self._index:
            result = self._index.query(
                namespace=namespace, vector=_embedding(query), top_k=min(limit, 50),
                include_metadata=True, filter=filters,
            )
            return [dict(match.metadata or {}) | {"score": match.score} for match in result.matches]

        q = _embedding(query)
        rows = [r for r in self._read_local() if r.get("namespace") == namespace and r.get("mobile_no") == mobile]
        if session_id:
            rows = [r for r in rows if r.get("session_id") == session_id]
        for row in rows:
            row["score"] = sum(a * b for a, b in zip(q, row.get("values", [])))
        rows.sort(key=lambda r: (r["score"], r["timestamp"]), reverse=True)
        return [{k: v for k, v in row.items() if k not in {"values", "namespace"}} for row in rows[:limit]]

    def recent(self, *, organization_id: str, mobile_no: str, limit: int = 30) -> list[dict[str, Any]]:
        items = self.search(
            organization_id=organization_id, mobile_no=mobile_no,
            query="issue request resolution preference", limit=limit,
        )
        return sorted(items, key=lambda item: item.get("timestamp", ""), reverse=True)

    @staticmethod
    def _read_local() -> list[dict[str, Any]]:
        if not DATA_FILE.exists():
            return []
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))

    @staticmethod
    def _write_local(rows: list[dict[str, Any]]) -> None:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def build_handoff_bullets(memories: list[dict[str, Any]]) -> list[str]:
    """Return exactly three agent-scannable bullets, newest evidence first."""
    customer = [m for m in memories if m.get("role") != "assistant"]
    source = customer or memories
    latest = source[0]["text"] if source else "No prior customer conversation is available."
    issue = next((m["text"] for m in source if re.search(r"issue|problem|wrong|help|bill|internet", m["text"], re.I)), latest)
    resolution = next((m["text"] for m in source if re.search(r"resolved|fixed|sorted|thank|still|again", m["text"], re.I)), None)
    sessions = len({m.get("session_id") for m in memories if m.get("session_id")})
    return [
        f"Current/last concern: {issue[:180]}",
        f"Outcome and sentiment: {(resolution or 'No confirmed resolution is recorded yet.')[:180]}",
        f"Relationship context: {sessions} prior session{'s' if sessions != 1 else ''}; latest message: {latest[:130]}",
    ]
