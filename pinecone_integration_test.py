"""Real Flask -> Mem0 -> Ollama -> Pinecone free-tier integration test."""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from ollama import Client

from flask_app import create_app
from mem0_memory import FreePineconeMem0MemoryStore


def main() -> None:
    load_dotenv()
    if not os.getenv("PINECONE_API_KEY"):
        raise RuntimeError("PINECONE_API_KEY is required in .env")

    os.environ["MEM0_LOCAL_INFER"] = "false"
    ollama = Client(host=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    llm_result = ollama.chat(
        model=os.getenv("MEM0_LOCAL_LLM_MODEL", "llama3.2:1b"),
        messages=[{"role": "user", "content": "Reply with only the word READY."}],
    )
    if not llm_result.message.content.strip():
        raise AssertionError("Local Ollama LLM returned no response")

    store = FreePineconeMem0MemoryStore()
    client = create_app(store).test_client()
    identity = {
        "organization_id": "pinecone-live-test",
        "mobile_no": "+923331234567",
    }

    saved = client.post("/api/tools/save_customer_memory/invoke", json={
        "arguments": {
            **identity,
            "session_id": "pinecone-live-session",
            "role": "customer",
            "text": "My fiber internet disconnects every evening and remains unresolved.",
        }
    })
    if saved.status_code != 200:
        raise AssertionError(f"Flask save tool failed: {saved.get_json()}")

    searched = client.post("/api/tools/search_customer_memory/invoke", json={
        "arguments": {
            **identity,
            "query": "What fiber internet problem did the customer report?",
            "limit": 5,
        }
    })
    if searched.status_code != 200:
        raise AssertionError(f"Flask search tool failed: {searched.get_json()}")
    memories = searched.get_json()["result"]["items"]
    if not memories:
        raise AssertionError("Pinecone returned no searchable memory")

    handoff = client.post("/api/tools/get_handoff_context/invoke", json={
        "arguments": identity
    })
    if handoff.status_code != 200:
        raise AssertionError(f"Flask handoff tool failed: {handoff.get_json()}")
    bullets = handoff.get_json()["result"]["history_summary"]
    if len(bullets) != 3:
        raise AssertionError("Handoff card must contain exactly three bullets")

    print(json.dumps({
        "status": "PASS",
        "backend": store.backend,
        "index": os.getenv("MEM0_FREE_PINECONE_INDEX", "quicktalk-mem0-free"),
        "organization_namespace": "org-pinecone-live-test",
        "local_llm_response": llm_result.message.content.strip(),
        "memories_found": len(memories),
        "handoff_bullets": bullets,
    }, indent=2))


if __name__ == "__main__":
    main()
