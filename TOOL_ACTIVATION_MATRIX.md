# Tool activation matrix

## Customer chat lifecycle

| Moment | Automatically activated | Purpose |
|---|---|---|
| Chat page opens or identity changes | `get_contextual_welcome` + tone profile | Load latest issue when available and write the greeting in the organization’s style. |
| Customer explicitly asks about prior/personal context | `search_customer_memory` | Search that customer’s earlier conversations semantically. General policy questions skip Mem0 and go directly to knowledge. |
| Customer memory has no answer | `resolve_support_answer` | Search bot KB first, then active approved human-agent knowledge, then apologize. Tone is applied to the final response every time. |
| After each customer/assistant message | `save_customer_memory` | Save tenant/customer/session-scoped conversational history to Mem0/Pinecone and the analytics event store. |
| Bot KB and agent knowledge both miss | `get_missing_knowledge_topics` data is recorded automatically | The resolver records a knowledge-gap event; the reporting tool later aggregates these questions. |
| Customer asks for a human | `get_handoff_context` | Return three curated bullets, counts and recent session summaries. |

## Human-agent and knowledge lifecycle

| Moment | Automatically activated | Purpose |
|---|---|---|
| Agent chat is recorded | Agent session/message APIs | Preserve the organization-owned source transcript. |
| Agent ends the chat | Strict curator using `prompts/knowledge_curator.txt` | Decide whether the transcript contains reusable organization knowledge. |
| Curator accepts | Mem0/Pinecone knowledge indexing | Store the standalone question/answer with organization, article and version metadata. |
| Curator rejects | No knowledge memory is created | Keep the transcript for review but reject filler, referrals, incomplete or customer-specific material. |
| Admin edits an article | New active version is indexed | Supersede the old version; retrieval uses only the active database version. |
| Admin disables/deletes | Retrieval status gate | Even a stale Pinecone vector is ignored because SQLite is authoritative. |
| Admin opens tool/testing UI | `get_organization_tone_profile`, `get_knowledge_curation_policy` on demand | Inspect style guidance and organization save/reject rules. |

## Tone activation rule

Tone is automatically applied to every customer-facing welcome, recalled-memory answer, bot-KB answer, approved-agent answer and no-answer apology. It is not applied to analytics, handoff facts, raw transcript storage, policy decisions or vector search. This keeps style separate from truth.

The prompt is `prompts/tone_response.txt`. It explicitly preserves every fact, number, currency, date, restriction and uncertainty. If Groq is unavailable, the original grounded answer is returned unchanged.

The live tone call uses one attempt with a two-second network timeout. It never enters the long retry queue used by offline curation/summarization, so a rate-limited tone service cannot block the live chat.

## Controlled organization facts

Human-agent chats may contribute reusable regulations, prices, fees and service policies, but these are controlled facts rather than permanent customer memories.

- Prices require currency, applicable product/service and effective/current period.
- Regulations and policies require organization scope, conditions and effective/version context.
- Service rules must be complete and applicable generally, not only to one customer/account.
- A later approved edit creates a new version; disabled or superseded versions are not used.
- Missing validity context causes rejection instead of guessing.

The default policy is returned by `get_knowledge_curation_policy`. Organization-specific overrides are stored in `knowledge_curation_policies` by `organization_scope` and are injected into the curator prompt.

## Prompt files

- `prompts/knowledge_curator.txt`: strict reusable-knowledge extraction and rejection rules.
- `prompts/tone_response.txt`: style-only rewrite with factual-preservation rules.
- Mem0 durable customer-fact extraction remains configured separately in `mem0_memory.py` because it serves conversational customer recall, not organization policy knowledge.
