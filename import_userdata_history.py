"""Import the supplied Mongo-style userData.history JSON into local analytics only."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from analytics import AnalyticsRepository


SOURCE = Path(r"D:\hp2\Downloads\last week data userData.history.json")

ORGANIZATIONS = {
    "Shifa_International": ("import-shifa", "shifa-international", "Shifa International", "healthcare"),
    "eshifa_international": ("import-eshifa", "eshifa-international", "eShifa International", "healthcare"),
    "shifa_international_agent": ("import-shifa-agent", "shifa-agent", "Shifa Agent Service", "healthcare"),
    "nayatel": ("import-nayatel", "nayatel", "Nayatel", "telecom"),
    "Transworld": ("import-transworld", "transworld", "Transworld", "telecom"),
}

FUNCTION_CATEGORIES = {
    "doctor_appointment": "Appointments", "appointment_lookup": "Appointments",
    "reschedule_appointment": "Appointments", "book_diagnostic_services": "Appointments",
    "get_consultation_fee": "Billing & Insurance", "mrno_lookup": "Appointments",
    "internet_status": "Connectivity", "generate_ticket_or_complaint": "Connectivity",
    "get_ticket_or_complaint_status": "Connectivity", "get_internet_plans": "Plan & Upgrade",
    "assign_plan": "Plan & Upgrade", "get_user_arrears": "Billing",
    "bill_information": "Billing",
}


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    with SOURCE.open(encoding="utf-8") as source_file:
        source_data = json.load(source_file)

    repository = AnalyticsRepository()
    scopes = {}
    for source_org, (tenant, organization, name, industry) in ORGANIZATIONS.items():
        scope = f"{tenant}--{organization}"
        scopes[source_org] = scope
        repository.register_organization(
            scope=scope, tenant_id=tenant, organization_id=organization,
            organization_name=name, industry=industry,
        )

    stats = Counter()
    stats["messages_processed"] = repository.record_memories_bulk(
        iter_events(source_data, scopes, stats)
    )
    report = {
        "status": "PASS", "source_file": SOURCE.name, "local_analytics_only": True,
        "source_records": len(source_data), "organizations": len(scopes), **stats,
    }
    print(json.dumps(report, indent=2))


def iter_events(source_data, scopes, stats):
    for record in source_data:
        source_org = str(record.get("organization_id") or "")
        if source_org not in scopes:
            stats["skipped_unknown_organization"] += 1
            continue
        oid = str((record.get("_id") or {}).get("$oid") or "unknown")
        mobile = normalize_pakistan_mobile(record.get("client_id") or record.get("username") or "")
        if not mobile:
            stats["skipped_invalid_mobile"] += 1
            continue
        base_session = str(record.get("session_id") or f"session-{oid}")
        session_id = f"{base_session}-{oid}"
        for index, exchange in enumerate(record.get("history") or []):
            functions = [str(value) for value in exchange.get("function_name") or []]
            category = next((FUNCTION_CATEGORIES[name] for name in functions if name in FUNCTION_CATEGORIES), None)
            question = str(exchange.get("question") or "").strip()
            answer = str(exchange.get("answer") or "").strip()
            if question:
                yield {
                    "id": f"import-{oid}-{index}-q", "organization_id": scopes[source_org],
                    "mobile_no": mobile, "session_id": session_id, "role": "customer",
                    "timestamp": exchange.get("question_timestamp") or timestamp_value(exchange),
                    "text": question, "category": category,
                }
                stats["customer_messages"] += 1
            if answer:
                yield {
                    "id": f"import-{oid}-{index}-a", "organization_id": scopes[source_org],
                    "mobile_no": mobile, "session_id": session_id, "role": "assistant",
                    "timestamp": exchange.get("answer_timestamp") or timestamp_value(exchange),
                    "text": answer, "category": category,
                }
                stats["assistant_messages"] += 1


def normalize_pakistan_mobile(value) -> str | None:
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("03") and len(digits) == 11:
        return "+92" + digits[1:]
    if digits.startswith("92") and 11 <= len(digits) <= 13:
        return "+" + digits
    if 7 <= len(digits) <= 15:
        return "+" + digits
    return None


def timestamp_value(exchange) -> str:
    timestamp = exchange.get("timestamp") or {}
    return str(timestamp.get("$date") or "2026-08-01T00:00:00+00:00")


if __name__ == "__main__":
    main()
