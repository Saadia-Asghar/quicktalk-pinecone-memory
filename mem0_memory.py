"""Mem0 OSS infrastructure adapter backed by organization-isolated Pinecone namespaces."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from pinecone_memory import _namespace, normalize_mobile, normalize_timestamp, validate_role


class Mem0MemoryStore:
    """Expose the same service contract as MemoryStore using Mem0 OSS.

    A separate Mem0 client is configured for every organization namespace. The
    Mem0 user_id is also an opaque hash of organization + mobile number, giving
    two independent tenant-isolation boundaries.
    """

    def __init__(self) -> None:
        required = ("PINECONE_API_KEY", "OPENAI_API_KEY")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Mem0 backend requires: {', '.join(missing)}")
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "mem0-pinecone"

    @property
    def infer_memories(self) -> bool:
        return True

    @staticmethod
    def _customer_id(organization_id: str, mobile_no: str) -> str:
        raw = f"{organization_id.strip()}:{normalize_mobile(mobile_no)}"
        return "customer-" + hashlib.sha256(raw.encode()).hexdigest()

    def _client(self, organization_id: str):
        namespace = _namespace(organization_id)
        with self._lock:
            if namespace not in self._clients:
                from mem0 import Memory

                dimensions = int(os.getenv("MEM0_EMBEDDING_DIMENSION", "1536"))
                config = {
                    "llm": {
                        "provider": "openai",
                        "config": {"model": os.getenv("MEM0_LLM_MODEL", "gpt-4.1-mini")},
                    },
                    "embedder": {
                        "provider": "openai",
                        "config": {"model": os.getenv("MEM0_EMBEDDING_MODEL", "text-embedding-3-small")},
                    },
                    "vector_store": {
                        "provider": "pinecone",
                        "config": {
                            "collection_name": os.getenv("MEM0_PINECONE_INDEX", "quicktalk-mem0"),
                            "embedding_model_dims": dimensions,
                            "namespace": namespace,
                            "serverless_config": {
                                "cloud": os.getenv("PINECONE_CLOUD", "aws"),
                                "region": os.getenv("PINECONE_REGION", "us-east-1"),
                            },
                            "metric": "cosine",
                        },
                    },
                    "history_db_path": os.getenv("MEM0_HISTORY_DB", "data/mem0_history.db"),
                    "custom_instructions": (
                        "Extract durable contact-center facts, customer preferences, active issues, "
                        "resolutions, and sentiment. Never merge identities or organizations."
                    ),
                }
                self._clients[namespace] = Memory.from_config(config)
            return self._clients[namespace]

    def add(self, *, organization_id: str, session_id: str, mobile_no: str,
            text: str, role: str = "customer", timestamp: str | None = None,
            infer: bool | None = None) -> dict[str, Any]:
        if not organization_id.strip() or not session_id.strip() or not text.strip():
            raise ValueError("organization_id, session_id and text are required")
        mobile = normalize_mobile(mobile_no)
        record = {
            "id": str(uuid.uuid4()),
            "organization_id": organization_id.strip(),
            "session_id": session_id.strip(),
            "mobile_no": mobile,
            "timestamp": normalize_timestamp(timestamp),
            "role": validate_role(role),
            "text": text.strip(),
        }
        mem0_role = "user" if role == "customer" else "assistant" if role == "assistant" else "system"
        infer_val = infer if infer is not None else self.infer_memories
        self._client(organization_id).add(
            [{"role": mem0_role, "content": record["text"]}],
            user_id=self._customer_id(organization_id, mobile),
            metadata=record,
            infer=infer_val,
        )
        return record

    def search(self, *, organization_id: str, mobile_no: str, query: str = "conversation history",
               session_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        mobile = normalize_mobile(mobile_no)
        customer_id = self._customer_id(organization_id, mobile)
        filters: dict[str, Any] = {}
        if session_id:
            filters["session_id"] = session_id
        response = self._client(organization_id).search(
            query,
            user_id=customer_id,
            filters=filters or None,
            limit=min(limit, 50),
        )
        results = response.get("results", []) if isinstance(response, dict) else response
        normalized = []
        for item in results or []:
            metadata = dict(item.get("metadata") or {})
            metadata["text"] = item.get("memory", metadata.get("text", ""))
            metadata["score"] = item.get("score", 0)
            normalized.append(metadata)
        return normalized

    def recent(self, *, organization_id: str, mobile_no: str, limit: int = 100) -> list[dict[str, Any]]:
        mobile = normalize_mobile(mobile_no)
        response = self._client(organization_id).get_all(
            user_id=self._customer_id(organization_id, mobile), limit=min(limit, 100)
        )
        results = response.get("results", []) if isinstance(response, dict) else response
        items = []
        for item in results or []:
            metadata = dict(item.get("metadata") or {})
            metadata["text"] = item.get("memory", metadata.get("text", ""))
            items.append(metadata)
        now = datetime.now(timezone.utc)
        filtered = []
        for item in items:
            ts = item.get("timestamp")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_seconds = (now - dt).total_seconds()
                    if 0 <= age_seconds <= 30 * 24 * 3600:
                        filtered.append(item)
                except ValueError:
                    filtered.append(item)
            else:
                filtered.append(item)
        return sorted(filtered, key=lambda item: item.get("timestamp", ""), reverse=True)


class FreeLocalMem0MemoryStore(Mem0MemoryStore):
    """Fully local Mem0 using Ollama and embedded Qdrant; no paid API keys."""

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "mem0-local-ollama-qdrant"

    @property
    def infer_memories(self) -> bool:
        return os.getenv("MEM0_LOCAL_INFER", "true").lower() == "true"

    def _client(self, organization_id: str):
        namespace = _namespace(organization_id)
        with self._lock:
            if namespace not in self._clients:
                from mem0 import Memory

                base_path = os.getenv("MEM0_LOCAL_QDRANT_PATH", "data/qdrant")
                config = {
                    "llm": {
                        "provider": "ollama",
                        "config": {
                            "model": os.getenv("MEM0_LOCAL_LLM_MODEL", "llama3.2:1b"),
                            "temperature": 0,
                            "max_tokens": 1200,
                            "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                        },
                    },
                    "embedder": {
                        "provider": "ollama",
                        "config": {
                            "model": os.getenv("MEM0_LOCAL_EMBEDDING_MODEL", "nomic-embed-text:latest"),
                            "embedding_dims": int(os.getenv("MEM0_LOCAL_EMBEDDING_DIMENSION", "768")),
                            "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                        },
                    },
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "collection_name": "mem0",
                            "embedding_model_dims": int(
                                os.getenv("MEM0_LOCAL_EMBEDDING_DIMENSION", "768")
                            ),
                            "path": os.path.join(base_path, namespace),
                            "on_disk": True,
                        },
                    },
                    "history_db_path": os.getenv(
                        "MEM0_LOCAL_HISTORY_DB", f"data/{namespace}-mem0-history.db"
                    ),
                    "custom_instructions": (
                        "Extract durable contact-center facts, customer preferences, active issues, "
                        "resolutions, and sentiment. Never merge identities or organizations."
                    ),
                }
                self._clients[namespace] = Memory.from_config(config)
            return self._clients[namespace]


class FreePineconeMem0MemoryStore(Mem0MemoryStore):
    """Mem0 with free local Ollama inference/embeddings and Pinecone vectors."""

    def __init__(self) -> None:
        if not os.getenv("PINECONE_API_KEY"):
            raise RuntimeError("Pinecone free mode requires PINECONE_API_KEY")
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "mem0-ollama-pinecone"

    @property
    def infer_memories(self) -> bool:
        return os.getenv("MEM0_LOCAL_INFER", "true").lower() == "true"

    def _client(self, organization_id: str):
        namespace = _namespace(organization_id)
        with self._lock:
            if namespace not in self._clients:
                from mem0 import Memory

                dimensions = int(os.getenv("MEM0_LOCAL_EMBEDDING_DIMENSION", "768"))
                config = {
                    "llm": {
                        "provider": "ollama",
                        "config": {
                            "model": os.getenv("MEM0_LOCAL_LLM_MODEL", "llama3.2:1b"),
                            "temperature": 0,
                            "max_tokens": 1200,
                            "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                        },
                    },
                    "embedder": {
                        "provider": "ollama",
                        "config": {
                            "model": os.getenv(
                                "MEM0_LOCAL_EMBEDDING_MODEL", "nomic-embed-text:latest"
                            ),
                            "embedding_dims": dimensions,
                            "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
                        },
                    },
                    "vector_store": {
                        "provider": "pinecone",
                        "config": {
                            "collection_name": os.getenv(
                                "MEM0_FREE_PINECONE_INDEX", "quicktalk-mem0-free"
                            ),
                            "embedding_model_dims": dimensions,
                            "namespace": namespace,
                            "serverless_config": {
                                "cloud": os.getenv("PINECONE_CLOUD", "aws"),
                                "region": os.getenv("PINECONE_REGION", "us-east-1"),
                            },
                            "metric": "cosine",
                        },
                    },
                    "history_db_path": os.getenv(
                        "MEM0_LOCAL_HISTORY_DB", "data/mem0-pinecone-history.db"
                    ),
                    "custom_instructions": (
                        "Extract durable contact-center facts, customer preferences, active issues, "
                        "resolutions, and sentiment. Never merge identities or organizations."
                    ),
                }
                self._clients[namespace] = Memory.from_config(config)
            return self._clients[namespace]


def create_memory_store():
    """Select the configured infrastructure without importing Mem0 unnecessarily."""
    backend = os.getenv("MEMORY_BACKEND", "pinecone").lower()
    if backend in ("mem0", "mem0-pinecone-free") and not os.getenv("PINECONE_API_KEY"):
        from pinecone_memory import MemoryStore
        return MemoryStore()
    if backend == "mem0":
        return Mem0MemoryStore()
    if backend == "mem0-local":
        return FreeLocalMem0MemoryStore()
    if backend == "mem0-pinecone-free":
        return FreePineconeMem0MemoryStore()
    from pinecone_memory import MemoryStore

    return MemoryStore()
