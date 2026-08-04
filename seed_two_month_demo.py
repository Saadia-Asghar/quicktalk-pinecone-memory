"""Load the two-month multi-tenant demo dataset through Flask tools into Pinecone."""

from __future__ import annotations

import os
import json
from pathlib import Path

from dotenv import load_dotenv

from flask_app import create_app
from mem0_memory import FreePineconeMem0MemoryStore


DATASET = Path(__file__).parent / "demo_data" / "two_month_tenant_history.json"


def main() -> None:
    load_dotenv()
    data = json.loads(DATASET.read_text(encoding="utf-8"))
    client = create_app(FreePineconeMem0MemoryStore()).test_client()
    saved = 0
    summaries = []

    for organization in data["organizations"]:
        scope = organization["pinecone_scope"]
        for customer in organization["customers"]:
            for session in customer["sessions"]:
                for message in session["messages"]:
                    response = client.post("/api/tools/save_customer_memory/invoke", json={
                        "arguments": {
                            "organization_id": scope,
                            "session_id": session["session_id"],
                            "mobile_no": customer["mobile_no"],
                            "timestamp": message["timestamp"],
                            "role": message["role"],
                            "text": message["text"],
                        }
                    }, headers={"X-API-Key": os.getenv("SERVICE_API_KEY", "")})
                    if response.status_code != 200:
                        raise RuntimeError(response.get_json())
                    saved += 1

            handoff = client.post("/api/tools/get_handoff_context/invoke", json={
                "arguments": {
                    "organization_id": scope,
                    "mobile_no": customer["mobile_no"],
                }
            }, headers={"X-API-Key": os.getenv("SERVICE_API_KEY", "")})
            result = handoff.get_json()["result"]
            summaries.append({
                "tenant_id": organization["tenant_id"],
                "organization_id": organization["organization_id"],
                "pinecone_scope": scope,
                "customer": customer["display_name"],
                "mobile_no": customer["mobile_no"],
                "memories_recalled": result["memory_count"],
                "handoff_bullets": result["history_summary"],
            })

    print(json.dumps({
        "status": "PASS",
        "period": data["period"],
        "organizations": len(data["organizations"]),
        "customers": sum(len(item["customers"]) for item in data["organizations"]),
        "messages_saved": saved,
        "demo_profiles": summaries,
    }, indent=2))


if __name__ == "__main__":
    main()
