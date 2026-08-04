"""Live negative isolation test against Pinecone without printing credentials."""

from __future__ import annotations

import json
import os
import uuid

from dotenv import load_dotenv

from flask_app import create_app
from mem0_memory import FreePineconeMem0MemoryStore


def main() -> None:
    load_dotenv()
    if not os.getenv("PINECONE_API_KEY"):
        raise RuntimeError("PINECONE_API_KEY is required")

    marker = uuid.uuid4().hex[:10]
    organization_a = f"negative-org-a-{marker}"
    organization_b = f"negative-org-b-{marker}"
    mobile_a = "+923331110001"
    mobile_b = "+923331110002"
    store = FreePineconeMem0MemoryStore()
    client = create_app(store).test_client()

    save = client.post("/api/tools/save_customer_memory/invoke", json={
        "arguments": {
            "organization_id": organization_a,
            "session_id": f"negative-session-{marker}",
            "mobile_no": mobile_a,
            "role": "customer",
            "text": f"Private billing marker {marker} must remain isolated.",
        }
    })
    assert save.status_code == 200, save.get_json()

    correct = _search(client, organization_a, mobile_a, marker)
    wrong_org = _search(client, organization_b, mobile_a, marker)
    wrong_mobile = _search(client, organization_a, mobile_b, marker)

    assert correct["count"] >= 1, "Correct customer could not recall its memory"
    assert wrong_org["count"] == 0, "FAIL: cross-organization memory leak"
    assert wrong_mobile["count"] == 0, "FAIL: cross-customer memory leak"

    missing = client.post("/api/tools/save_customer_memory/invoke", json={
        "arguments": {"organization_id": organization_a}
    })
    assert missing.status_code == 400, "Missing arguments were not rejected"

    print(json.dumps({
        "status": "PASS",
        "backend": store.backend,
        "correct_customer_results": correct["count"],
        "wrong_organization_results": wrong_org["count"],
        "wrong_mobile_results": wrong_mobile["count"],
        "missing_arguments_status": missing.status_code,
        "api_key_exposed": False,
    }, indent=2))


def _search(client, organization_id: str, mobile_no: str, marker: str):
    response = client.post("/api/tools/search_customer_memory/invoke", json={
        "arguments": {
            "organization_id": organization_id,
            "mobile_no": mobile_no,
            "query": f"Find private billing marker {marker}",
            "limit": 5,
        }
    })
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]


if __name__ == "__main__":
    main()
