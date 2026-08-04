"""Verify the seeded demo records and tenant isolation against live Pinecone."""

from __future__ import annotations

import json

from dotenv import load_dotenv

from flask_app import create_app
from mem0_memory import FreePineconeMem0MemoryStore


def search(client, organization_id: str, mobile_no: str, query: str) -> int:
    import os
    response = client.post(
        "/api/tools/search_customer_memory/invoke",
        json={
            "arguments": {
                "organization_id": organization_id,
                "mobile_no": mobile_no,
                "query": query,
                "limit": 5,
            }
        },
        headers={"X-API-Key": os.getenv("SERVICE_API_KEY", "")}
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["result"]["count"]


def main() -> None:
    load_dotenv()
    client = create_app(FreePineconeMem0MemoryStore()).test_client()

    northstar_scope = "tenant-northstar-001--northstar-telecom"
    urbanpay_scope = "tenant-urbanpay-002--urbanpay-wallet"
    correct_northstar = search(
        client, northstar_scope, "+923331110001", "evening internet disconnection"
    )
    correct_urbanpay = search(
        client, urbanpay_scope, "+923332220001", "unauthorized charge"
    )
    crossed_customer = search(
        client, northstar_scope, "+923332220001", "unauthorized charge"
    )

    assert correct_northstar > 0, "Northstar demo history was not found"
    assert correct_urbanpay > 0, "UrbanPay demo history was not found"
    assert crossed_customer == 0, "Cross-tenant customer data leaked"

    print(
        json.dumps(
            {
                "status": "PASS",
                "northstar_results": correct_northstar,
                "urbanpay_results": correct_urbanpay,
                "cross_tenant_results": crossed_customer,
                "tenant_isolation": "PASS",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
