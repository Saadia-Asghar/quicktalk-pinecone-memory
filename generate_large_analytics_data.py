"""Generate a deterministic, synthetic three-month multi-industry JSON dataset."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path


OUTPUT = Path(__file__).parent / "demo_data" / "three_month_large_analytics.json"
RANDOM = random.Random(20260803)

ORGANIZATIONS = [
    {
        "tenant_id": "tenant-northstar-001", "organization_id": "northstar-telecom",
        "organization_name": "Northstar Telecom Demo",
        "organization_scope": "tenant-northstar-001--northstar-telecom", "industry": "telecom",
    },
    {
        "tenant_id": "tenant-urbanpay-002", "organization_id": "urbanpay-wallet",
        "organization_name": "UrbanPay Wallet Demo",
        "organization_scope": "tenant-urbanpay-002--urbanpay-wallet", "industry": "fintech",
    },
    {
        "tenant_id": "tenant-carepoint-003", "organization_id": "carepoint-clinic",
        "organization_name": "CarePoint Clinic Demo",
        "organization_scope": "tenant-carepoint-003--carepoint-clinic", "industry": "healthcare",
    },
]

SCENARIOS = {
    "telecom": [
        ("connectivity", "My fiber internet disconnects repeatedly in the evening.", "Technical support opened a line-monitoring case.", "The connection is fixed and stable now."),
        ("billing", "My monthly bill contains an unexpected equipment charge.", "Billing opened a charge review case.", "The incorrect charge was reversed and the billing issue is resolved."),
        ("plan_upgrade", "I want to upgrade my internet plan and confirm the price.", "The available upgrade and monthly price were shared.", "The upgraded plan is active now and I am satisfied."),
        ("installation", "My new router installation has not been completed.", "An installation technician visit was scheduled.", "The router installation was completed successfully."),
        ("speed", "My internet speed is much lower than the subscribed package.", "A speed diagnostic and port refresh were requested.", "The speed problem is still happening and needs urgent help."),
    ],
    "fintech": [
        ("payments", "A merchant payment failed but my wallet balance was deducted.", "A payment dispute was opened for investigation.", "The deducted balance was restored and the payment issue is resolved."),
        ("transfers", "My bank transfer is pending and the recipient has not received it.", "The transfer trace was submitted to the banking partner.", "The transfer was completed and received by the recipient."),
        ("fraud", "I found an unauthorized charge in my wallet account.", "The account was secured and a fraud dispute was opened.", "The unauthorized charge investigation is still pending and I am worried."),
        ("kyc", "My identity verification keeps failing during KYC.", "The KYC documents were sent for manual verification.", "Verification was completed and the account is active now."),
        ("login", "I cannot log in after changing my mobile device.", "A secure device reset link was issued.", "Login access is restored and the issue is fixed."),
    ],
    "healthcare": [
        ("respiratory", "I need an appointment for a persistent cough and breathing difficulty.", "A respiratory clinic appointment request was submitted.", "The doctor visit was completed and medication was prescribed."),
        ("cardiology", "I need a follow-up for high blood pressure and chest discomfort.", "A cardiology follow-up appointment was scheduled.", "The cardiology visit is completed and the follow-up plan was received."),
        ("dermatology", "My skin rash is getting worse and I need a dermatology appointment.", "The dermatology team was asked for the earliest appointment.", "The appointment is still pending and the rash remains unresolved."),
        ("medication", "I need an urgent refill of my regular medication.", "The prescription refill request was forwarded to the doctor.", "The prescription refill was completed and received."),
        ("insurance", "My insurance claim for the clinic visit was rejected.", "The insurance documents were sent to the billing team for review.", "The insurance claim review is still pending and I need help."),
    ],
}


def main() -> None:
    events = []
    months = [(2026, 5), (2026, 6), (2026, 7)]
    for org_index, organization in enumerate(ORGANIZATIONS, start=1):
        customers = [f"+923{org_index}{customer:08d}" for customer in range(1, 21)]
        for year, month in months:
            month_start = datetime(year, month, 1, 8, tzinfo=timezone.utc)
            for customer_index, mobile in enumerate(customers, start=1):
                for visit in range(2):
                    scenario = SCENARIOS[organization["industry"]][
                        (customer_index + visit + month) % len(SCENARIOS[organization["industry"]])
                    ]
                    day_offset = (customer_index * 2 + visit * 7) % 27
                    started = month_start + timedelta(days=day_offset, hours=RANDOM.randint(0, 9))
                    session_id = f"{organization['industry']}-{year}{month:02d}-{customer_index:02d}-{visit + 1}"
                    resolved = RANDOM.random() < 0.68
                    outcome = scenario[3] if resolved else unresolved_outcome(organization["industry"], scenario[0])
                    texts = [("customer", scenario[1]), ("assistant", scenario[2]), ("customer", outcome)]
                    for message_index, (role, text) in enumerate(texts, start=1):
                        timestamp = started + timedelta(minutes=(message_index - 1) * RANDOM.randint(3, 12))
                        events.append(
                            {
                                "id": f"bulk-{organization['industry']}-{year}{month:02d}-{customer_index:02d}-{visit + 1}-{message_index}",
                                "organization_scope": organization["organization_scope"],
                                "mobile_no": mobile,
                                "session_id": session_id,
                                "role": role,
                                "timestamp": timestamp.isoformat(),
                                "text": text,
                                "source_category": scenario[0],
                                "expected_resolution": "resolved" if resolved else "not_recorded",
                            }
                        )

    document = {
        "dataset": "QuickTalk large three-month multi-industry analytics demo",
        "synthetic": True,
        "period": {"from": "2026-05-01", "to": "2026-07-31"},
        "organizations": ORGANIZATIONS,
        "statistics": {
            "organization_count": len(ORGANIZATIONS),
            "customers_per_organization": 20,
            "sessions_per_customer_per_month": 2,
            "messages_per_session": 3,
            "total_sessions": len(events) // 3,
            "total_memory_events": len(events),
        },
        "events": events,
    }
    OUTPUT.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(json.dumps(document["statistics"], indent=2))


def unresolved_outcome(industry: str, category: str) -> str:
    return {
        "telecom": f"The {category.replace('_', ' ')} issue is still unresolved and I am frustrated.",
        "fintech": f"The {category.replace('_', ' ')} request is still pending and needs urgent attention.",
        "healthcare": f"The {category.replace('_', ' ')} request is still pending and I need a follow-up.",
    }[industry]


if __name__ == "__main__":
    main()
