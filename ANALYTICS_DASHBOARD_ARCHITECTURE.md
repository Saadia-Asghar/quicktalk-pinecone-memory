# Organization memory analytics

## Purpose

The dashboard converts tenant-scoped conversational memory into operational
metrics. Pinecone remains responsible for semantic recall. SQLite is the local
demo analytics store; production should use PostgreSQL, ClickHouse, or a data
warehouse depending on volume.

## Data flow

1. The application invokes `save_customer_memory`.
2. Flask validates organization, customer, session, role, text, and timestamp.
3. Mem0 stores semantic memory in the organization's Pinecone namespace.
4. The structured event is also written to the analytics repository.
5. An industry taxonomy classifies the event category.
6. The dashboard API aggregates only rows matching the requested organization.

## Current industry taxonomies

- Telecom: connectivity, billing, plan/upgrade, installation.
- Fintech: payments, transfers, disputes/fraud, KYC/account.
- Healthcare: respiratory, cardiology, dermatology, medication, appointments,
  billing/insurance.
- Generic: service issue, billing, request, other.

## Operator questions answered

- How many customers and sessions contacted us?
- What issues are most common?
- What percentage of sessions contain explicit resolution evidence?
- Which sessions still need action?
- Is customer sentiment trending negative?
- On which days did conversation volume rise?

## Patient and customer segmentation

Do not infer sensitive patient attributes from free text. If reporting needs
segments such as pediatric/adult, inpatient/outpatient, new/returning, or clinic
location, the source application must send approved structured metadata. Access
must follow the organization's consent, privacy, retention, and audit policies.

## Production changes required

- Replace the shared local demo key with authenticated user/organization claims.
- Enforce organization scope server-side from those claims; do not trust a query
  parameter or browser-provided tenant ID.
- Replace SQLite with a managed analytical database.
- Use a transactional outbox or event stream so Pinecone and analytics writes
  cannot silently diverge.
- Version taxonomies and retain classifier confidence/manual override.
- Add role-based access control, audit logs, encryption, deletion workflows, and
  healthcare compliance controls where applicable.
- Use scheduled materialized aggregates for large datasets.

## Useful next analytics

- repeat-contact rate and first-contact resolution;
- average time to resolution and aging unresolved cases;
- escalation rate by category, channel, team, product, or location;
- recurring incident detection and sudden category spikes;
- agent workload, response time, and handoff quality;
- recommended FAQ, automation, staffing, and product fixes based on recurring
  issues;
- cohort retention/satisfaction using explicitly collected, approved dimensions.
