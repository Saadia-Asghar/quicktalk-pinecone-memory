# Two-month multi-tenant demo data

The JSON dataset covers June and July 2026 for two isolated tenants, four
customers, eight sessions, and 24 messages.

## Load into live Pinecone

```powershell
C:\tmp\quicktalk-free-venv\Scripts\python.exe seed_two_month_demo.py
```

## Demo profiles

| Tenant | Organization | Pinecone organization scope | Customer | Mobile |
|---|---|---|---|---|
| `tenant-northstar-001` | `northstar-telecom` | `tenant-northstar-001--northstar-telecom` | Sadia Khan | `+923331110001` |
| `tenant-northstar-001` | `northstar-telecom` | `tenant-northstar-001--northstar-telecom` | Ali Raza | `+923331110002` |
| `tenant-urbanpay-002` | `urbanpay-wallet` | `tenant-urbanpay-002--urbanpay-wallet` | Fatima Noor | `+923332220001` |
| `tenant-urbanpay-002` | `urbanpay-wallet` | `tenant-urbanpay-002--urbanpay-wallet` | Hamza Ahmed | `+923332220002` |

Use the **Pinecone organization scope** value in the custom demo's Organization
field. Use the matching mobile number, then click **Escalate to human** to recall
the two-month history.

## Suggested checks

- Sadia: June billing charge resolved; July evening internet issue unresolved.
- Ali: June speed upgrade resolved; July tax certificate resolved.
- Fatima: June failed payment resolved; July unauthorized charge still pending.
- Hamza: June KYC issue resolved; July bank transfer resolved.

For isolation proof, combine Northstar's scope with an UrbanPay mobile number.
The result must contain zero memories.
