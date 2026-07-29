"""Real zero-cost integration test: Flask store contract -> Mem0 -> Ollama -> Qdrant."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ollama import Client

from flask_app import create_app
from mem0_memory import FreeLocalMem0MemoryStore


def main() -> None:
    test_root = Path(tempfile.mkdtemp(prefix="quicktalk-free-test-"))
    os.environ["MEM0_LOCAL_QDRANT_PATH"] = str(test_root / "qdrant")
    os.environ["MEM0_LOCAL_HISTORY_DB"] = str(test_root / "history.db")
    os.environ.setdefault("MEM0_LOCAL_LLM_MODEL", "llama3.2:1b")
    os.environ.setdefault("MEM0_LOCAL_EMBEDDING_MODEL", "nomic-embed-text:latest")
    os.environ.setdefault("MEM0_LOCAL_EMBEDDING_DIMENSION", "768")
    os.environ["MEM0_LOCAL_INFER"] = "false"

    ollama = Client(host=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    llm_result = ollama.chat(
        model=os.environ["MEM0_LOCAL_LLM_MODEL"],
        messages=[{"role": "user", "content": "Reply with only the word READY."}],
    )
    if not llm_result.message.content.strip():
        raise AssertionError("Local Ollama LLM returned no response")

    store = FreeLocalMem0MemoryStore()
    client = create_app(store).test_client()
    saved_response = client.post("/api/tools/save_customer_memory/invoke", json={
        "arguments": {
            "organization_id": "free-test-org",
            "session_id": "free-test-session",
            "mobile_no": "+923331234567",
            "role": "customer",
            "text": "My home internet disconnects every evening and the issue is still unresolved.",
        }
    })
    if saved_response.status_code != 200:
        raise AssertionError(f"Flask save tool failed: {saved_response.get_json()}")
    saved = saved_response.get_json()["result"]

    search_response = client.post("/api/tools/search_customer_memory/invoke", json={
        "arguments": {
            "organization_id": "free-test-org",
            "mobile_no": "+923331234567",
            "query": "What internet problem did the customer report?",
            "limit": 5,
        }
    })
    if search_response.status_code != 200:
        raise AssertionError(f"Flask search tool failed: {search_response.get_json()}")
    found = search_response.get_json()["result"]["items"]
    if not found:
        raise AssertionError("Mem0/Ollama/Qdrant returned no searchable memory")

    handoff_response = client.post("/api/tools/get_handoff_context/invoke", json={
        "arguments": {
            "organization_id": "free-test-org",
            "mobile_no": "+923331234567",
        }
    })
    if handoff_response.status_code != 200:
        raise AssertionError(f"Flask handoff tool failed: {handoff_response.get_json()}")
    summary = handoff_response.get_json()["result"]["history_summary"]
    if len(summary) != 3:
        raise AssertionError("Handoff card must contain exactly three bullets")

    print(json.dumps({
        "status": "PASS",
        "backend": store.backend,
        "local_llm_response": llm_result.message.content.strip(),
        "saved_session": saved["session_id"],
        "memories_found": len(found),
        "handoff_bullets": summary,
        "temporary_data_path": str(test_root),
    }, indent=2))


if __name__ == "__main__":
    main()
