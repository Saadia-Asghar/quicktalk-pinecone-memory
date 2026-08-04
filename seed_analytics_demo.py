"""Idempotently build organization analytics from demo conversation data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from analytics import AnalyticsRepository


ROOT = Path(__file__).parent
INDUSTRIES = {"northstar-telecom": "telecom", "urbanpay-wallet": "fintech"}


def main() -> None:
    repository = AnalyticsRepository()
    conversation_data = json.loads(
        (ROOT / "demo_data" / "two_month_tenant_history.json").read_text(encoding="utf-8")
    )
    loaded = 0
    for organization in conversation_data["organizations"]:
        scope = organization["pinecone_scope"]
        repository.register_organization(
            scope=scope,
            tenant_id=organization["tenant_id"],
            organization_id=organization["organization_id"],
            organization_name=organization["organization_name"],
            industry=INDUSTRIES[organization["organization_id"]],
        )
        for customer in organization["customers"]:
            for session in customer["sessions"]:
                for message in session["messages"]:
                    raw_id = f"{scope}:{customer['mobile_no']}:{session['session_id']}:{message['timestamp']}"
                    repository.record_memory(
                        {
                            "id": "analytics-" + hashlib.sha256(raw_id.encode()).hexdigest()[:24],
                            "organization_id": scope,
                            "mobile_no": customer["mobile_no"],
                            "session_id": session["session_id"],
                            **message,
                        }
                    )
                    loaded += 1

    healthcare = json.loads(
        (ROOT / "demo_data" / "healthcare_analytics.json").read_text(encoding="utf-8")
    )
    repository.register_organization(
        scope=healthcare["organization_scope"], tenant_id=healthcare["tenant_id"],
        organization_id=healthcare["organization_id"],
        organization_name=healthcare["organization_name"], industry=healthcare["industry"],
    )
    for event in healthcare["events"]:
        repository.record_memory({"organization_id": healthcare["organization_scope"], **event})
        loaded += 1

    large_path = ROOT / "demo_data" / "three_month_large_analytics.json"
    if large_path.exists():
        large = json.loads(large_path.read_text(encoding="utf-8"))
        for organization in large["organizations"]:
            repository.register_organization(
                scope=organization["organization_scope"], tenant_id=organization["tenant_id"],
                organization_id=organization["organization_id"],
                organization_name=organization["organization_name"],
                industry=organization["industry"],
            )
        for event in large["events"]:
            repository.record_memory(
                {"organization_id": event["organization_scope"], **event}
            )
            loaded += 1

    print(json.dumps({"status": "PASS", "organizations": 3, "events_processed": loaded}, indent=2))


if __name__ == "__main__":
    main()
