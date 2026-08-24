# QuickTalk Memory and Agent Knowledge Platform

## Executive summary

This Flask service gives a support bot four capabilities: cross-session customer recall, contextual welcomes, concise human handoff context, and organization-specific analytics. It also converts only high-quality human-agent resolutions into reusable knowledge. Every operation is tenant-scoped by `organization_id`; customer history is additionally scoped by normalized mobile number.

The bot resolves a support question in this order: bot knowledge base, active approved human-agent knowledge, then a safe apology. Failed questions are recorded as missing-knowledge analytics. Human-agent style is learned through a separate tone tool and is never treated as factual knowledge.

## Runtime architecture

1. The client calls Flask on port 8765.
2. Flask validates the service key, payload, organization scope and role.
3. The tool registry dispatches the named operation.
4. Mem0 extracts/retrieves conversational memories; Pinecone stores searchable vectors in organization namespaces.
5. SQLite stores authoritative structured state for the demo: sessions, messages, profiles, analytics, article versions, status and audit events.
6. Groq performs language tasks. Separate workload keys prevent knowledge curation from competing with memory extraction and summaries.
7. The response includes grounding/source metadata so the caller can distinguish bot KB, agent knowledge and memory.

## What each component does

### Flask

Flask exposes pages and JSON APIs, validates requests, enforces tenant scope, and connects tools to storage. Main entry point: `flask_app.py`.

### Mem0

Mem0 is the conversational-memory orchestration layer. It extracts durable facts from chat, associates memories with organization/customer/session metadata and performs semantic recall. Mem0 does not decide that every human-agent reply is reusable policy knowledge; the strict knowledge curator handles that separately.

### Pinecone

Pinecone stores embeddings for semantic search. Organization identifiers become namespaces, preventing cross-tenant vector search. Customer memories also carry mobile number and session metadata. Approved knowledge uses a reserved knowledge identity and article/version metadata.

### SQLite demo database

`data/analytics.db` is the authoritative demo store for relational queries and lifecycle state. Tables cover organizations, memory events, profiles, session summaries, agent sessions/messages, bot knowledge, versioned agent knowledge, policies and audit logs. Pinecone is not used as the system of record because vector stores are not suited to transactions, joins, unique constraints, status/version management or dashboard aggregation. Production should replace SQLite with PostgreSQL and optionally a warehouse.

### Groq

Groq is used for language understanding/generation, not storage. `GROQ_MEMORY_API_KEY` is used by Mem0 extraction. `GROQ_SUMMARY_API_KEY` is used for summaries/welcomes. `GROQ_KNOWLEDGE_API_KEY` is used for strict knowledge curation and grounded KB wording. `GROQ_API_KEY` remains a fallback only. Keys are server-side in ignored `.env` and must never be sent to the browser or committed.

## Strict reusable-knowledge policy

Save only complete, generally applicable factual answers; resolved procedures with required steps or conditions; and standalone question/answer pairs that remain clear outside the original chat.

Reject greetings, thanks, urgency and filler; clarification questions; transfers/referrals; promises to reply later; unresolved or tentative answers; missing requested details; ambiguous references; customer-specific facts/identifiers; guesses and unsafe claims.

Example rejected: customer asks for the price of a 20-to-30 Mbps upgrade and the agent says “billing will assist you in morning.” It is a referral and does not contain the requested price. Example accepted: customer asks whether weekend installation is available and the agent gives a complete condition such as availability after serviceability and scheduling confirmation.

At chat close, the LLM returns structured JSON containing `reusable`, standalone `question`, standalone `answer`, `reason` and `confidence`. A deterministic validator runs after the LLM. Only pairs passing both stages become an active article and are indexed into Mem0/Pinecone. A rejected chat remains in the agent transcript but produces no reusable article.

## Separate tone-learning tool

`get_organization_tone_profile` studies recent human-agent messages for style signals such as reply length, politeness and Roman Urdu usage. It returns writing guidance with `facts_learned: false`. It must never copy customer data, claims, promises, mistakes or identifiers. The orchestration layer can give this style guidance to the response LLM after factual retrieval.

Tone is automatically applied to every customer-facing welcome, recalled-memory answer, bot-KB answer, approved-agent answer and safe apology. It is not applied to storage, retrieval ranking, analytics or knowledge approval. The source-controlled prompts are `prompts/tone_response.txt` and `prompts/knowledge_curator.txt`.

## Tool catalog

### save_customer_memory

Purpose: save one customer/assistant message with organization, session, mobile, role and timestamp. Storage: Mem0/Pinecone plus SQLite analytics event. Example arguments: `{"organization_id":"nayatel","session_id":"s-100","mobile_no":"+923001234567","role":"customer","text":"I prefer evening appointments"}`.

### search_customer_memory

Purpose: semantic recall for one customer inside one organization. It can optionally produce a grounded response. Example query: “Which doctor did I discuss last month?” The search cannot access another mobile number or organization.

### get_customer_memory_context

Purpose: load prior-session context at chat start. Returns memory count, previous-session count, current issue and recent session summaries from the precomputed profile, with a tenant-scoped memory fallback.

### get_contextual_welcome

Purpose: create a returning-customer welcome using the latest useful session issue. Example: “Welcome back. Is your evening disconnection issue resolved, or do you need more help?” It falls back to a neutral greeting when no history exists.

### get_handoff_context

Purpose: provide the human agent with three concise history bullets plus recent session summaries and counts. It uses the precomputed SQLite profile for speed; older details load only when expanded.

### get_organization_tone_profile

Purpose: return organization-specific style guidance only. It does not store or return factual support answers.

### get_knowledge_curation_policy

Purpose: expose the organization-scoped save/reject policy used before agent knowledge is indexed.

### search_approved_knowledge

Purpose: search active, approved and current-version human-agent knowledge. Pinecone finds semantic candidates; SQLite verifies organization, article status and exact active version before an answer is allowed.

### resolve_support_answer

Purpose: production answer path. It searches bot KB first, approved agent knowledge second and returns an apology if both miss. A miss creates a knowledge-gap analytics event.

### get_missing_knowledge_topics

Purpose: aggregate unanswered live queries by tenant so the organization can identify KB gaps. It uses deterministic database aggregation and does not need a Groq call.

### import_agent_history_from_json

Purpose: start the controlled background importer for historical human-agent transcripts. This is an administrative operation, not a normal live-chat call. Production should move it to a queue/worker with job IDs, idempotency and rate controls.

## Generic tool API

Endpoint: `POST /api/tools/{tool_name}/invoke`. Headers: `Content-Type: application/json`, `X-API-Key: <service-key>`, `X-Organization-Scope: <organization-id>`. Body: `{"arguments":{...}}`.

Example:

```bash
curl -X POST http://127.0.0.1:8765/api/tools/resolve_support_answer/invoke \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $SERVICE_API_KEY" \
  -H "X-Organization-Scope: nayatel" \
  -d '{"arguments":{"organization_id":"nayatel","session_id":"s-100","mobile_no":"+923001234567","query":"Can I upgrade to 30 Mbps?"}}'
```

## Important REST APIs and pages

- `GET /api/health`: backend and cache health.
- `GET /api/tools`: protected machine-readable tool schemas.
- `GET /api/public/tool-catalog`: public schemas without secrets.
- `POST /api/memories`: direct memory ingestion.
- `GET /api/profiles/{mobile}`: optimized customer profile.
- `GET /api/analytics/dashboard?organization_id=...&days=...`: tenant analytics.
- `POST /api/agent-chats`, `/messages`, `/close`: agent transcript lifecycle and strict curation.
- `GET/PATCH/POST /api/knowledge/articles...`: review, edit, disable or delete versioned knowledge.
- `/custom`: customer chat and handoff demo.
- `/knowledge`: human-agent transcript and knowledge portal.
- `/dashboard`: organization analytics.
- `/tools`: tool catalog and testing page.

## Data isolation and version safety

Tenant scope must come from authenticated claims in production, not a user-editable field. The service currently rejects mismatches between `X-Organization-Scope` and tool arguments. Pinecone namespaces isolate organizations. SQLite queries always include organization scope. Editing creates a new active article version and supersedes the old version. Disabling/deleting changes authoritative status. Even if an old vector remains in Pinecone, retrieval rejects it when SQLite says it is inactive or an obsolete version.

## Analytics flow

Every message creates structured analytics fields such as category, sentiment and resolution state. Session-close processing generates a concise session summary. Dashboard queries aggregate SQLite rows by organization and time range. When `resolve_support_answer` cannot find grounded evidence in either knowledge source, it records the exact question and topic as a missing-knowledge event. This keeps analytics fast and auditable; Groq may classify/summarize sessions, while counts and filters remain deterministic database operations.

## Performance design

Profiles and session summaries are computed when new events arrive rather than rebuilding full history at every handoff. Active profiles can be cached locally or in Redis. Pinecone retrieves only relevant vectors. SQLite indexes cover organization/status/time and article/version lookups. Separate Groq workload keys reduce shared rate-limit contention, but they do not guarantee zero latency; all external APIs still require timeouts, retry limits and graceful fallback.

## Testing checklist

1. Health returns 200 and reports the expected memory backend.
2. Both Groq workload keys make independent successful calls; never print the keys.
3. Save and search a customer memory; confirm another tenant/mobile cannot retrieve it.
4. Get context and welcome for existing and new customers.
5. Request handoff and verify three concise bullets/session counts.
6. Close a good agent chat and verify an active versioned article is created.
7. Close a referral/filler chat and verify `reusable:false`, `article:null` and a rejection reason.
8. Edit an article and verify only the new version is used; disable it and verify retrieval refuses it.
9. Resolve a bot-KB hit, an agent-KB fallback hit and a total miss/apology.
10. Verify the total miss appears in missing-topic analytics for that organization only.
11. Invoke tone profile and confirm `facts_learned:false`.
12. Test wrong service key, missing fields, invalid timestamp/role and tenant mismatch.

## Production readiness

The demo is locally testable, but production needs PostgreSQL, authenticated tenant/user claims, a secrets manager, Redis for shared cache, a queue/outbox for Pinecone synchronization and imports, rate limiting, monitoring, backups, retention/deletion policy, encryption and a dead-letter/reconciliation process. Rotate any API key ever shared in chat before deployment. Never commit `.env`.
