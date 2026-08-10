# Missing-Topic Agent Chat Learning Tool — Implementation Plan

## Objective

Build one Flask tool that takes a confirmed missing-knowledge question, searches tenant-isolated human-agent chats for evidence of a successful answer, uses an LLM to draft a grounded answer with citations, and publishes it to the bot knowledge base only after approval.

This is retrieval-augmented knowledge creation, not model fine-tuning. Fine-tuning can be considered later after a large approved dataset exists.

## Non-negotiable invariants

- Never search across organizations.
- Never publish raw agent text directly as bot knowledge.
- Remove or mask phone numbers, patient identifiers, tokens, addresses, and other private data before LLM use or publication.
- A missing-knowledge question must be supported by an explicit bot refusal, not merely an escalation.
- A candidate answer must cite the agent sessions that support it.
- If evidence conflicts, is incomplete, or is below threshold, return `needs_review`; do not answer automatically.
- Only approved knowledge may be used for production bot responses.
- Confirmed appointments, payments, clinical facts, and ticket status must still be verified against the operational database/tool.

## Proposed Flask tool

Name: `research_missing_topic`

Input:

```json
{
  "knowledge_gap_id": "gap-uuid",
  "max_agent_sessions": 10
}
```

The server derives organization, question, and reviewer permissions from authenticated claims and the stored gap. They are never trusted from browser input.

Output:

```json
{
  "status": "candidate_found",
  "normalized_topic": "Treatment X availability",
  "candidate_answer": "The hospital provides ...",
  "confidence": 0.87,
  "evidence": [
    {
      "session_id": "agent-session-123",
      "message_id": "message-456",
      "excerpt": "...",
      "outcome": "resolved"
    }
  ],
  "conflicts": [],
  "publication_status": "pending_review"
}
```

Allowed terminal statuses: `candidate_found`, `no_evidence`, `conflicting_evidence`, `needs_review`, `approved`, `rejected`.

## Data flow

```text
Explicit missing-knowledge event
        ↓
Normalize question and topic with LLM
        ↓
Tenant-scoped semantic search over human-agent chats
        ↓
Filter: agent-authored + resolved/successful sessions + recent/valid
        ↓
Rerank evidence and detect conflicts
        ↓
Mask private/customer-specific values
        ↓
LLM drafts answer using evidence only, with citations
        ↓
Confidence and policy gate
        ↓
Human approval
        ↓
Authoritative-source verification
        ↓
Publish versioned knowledge article
        ↓
Bot retrieval uses approved article on future matching questions
```

## Component responsibility

| Component | Responsibility |
|---|---|
| Mem0/Pinecone | Semantic retrieval of relevant agent-chat evidence and later retrieval of approved conversational knowledge |
| LLM | Normalize the question, rerank evidence, identify contradictions, synthesize a concise answer, and assign a reasoned confidence |
| PostgreSQL/SQLite | Source records for gaps, evidence links, review status, knowledge article versions, audit history, and analytics |
| Flask | Tenant validation, tool orchestration, authorization, thresholds, error handling, and API contract |
| Human reviewer | Approve, edit, or reject candidate knowledge before production use |

## Step 0 — Define trust, authentication, privacy, and lifecycle boundaries

Dependencies: none. Every other step depends on this step.

- Derive tenant, user, and role exclusively from authenticated server claims.
- Define roles and permissions for research, evidence viewing, editing, approval, publication, revocation, and audit access.
- Classify raw chats, embeddings, prompts, excerpts, candidate answers, and approved articles by sensitivity.
- Require redaction before embedding or LLM submission; scrub logs; encrypt stored source references; use keyed HMACs rather than predictable plain hashes.
- Define provider data-processing, residency, consent, retention, legal-hold, and deletion requirements.
- Define separate state machines for gaps, research runs, candidates, reviews, articles, and indexing jobs, including optimistic locking and allowed transitions.
- Threat-model forged tenant IDs, enumeration, prompt injection in chats, poisoned agent answers, malicious reviewers, and partial publication failures.

Exit criteria:

- Authorization matrix and state transitions are documented and tested.
- Tenant cannot be supplied or overridden through the tool payload.
- Privacy/security owners approve the data flow before agent chats are indexed.

## Step 1 — Add schemas and repository methods

Dependencies: Step 0.

Add tables (SQLite demo, PostgreSQL-compatible design):

- `knowledge_gaps`: gap ID, organization, exact question, topic, evidence phrase, session, timestamps, status.
- `knowledge_candidates`: candidate ID, gap ID, answer, confidence, conflicts, model/provider, prompt version, review state.
- `knowledge_evidence`: candidate ID, source session/message, excerpt hash, resolution status, relevance score.
- `knowledge_articles`: article ID, organization, canonical question/topic, approved answer, version, effective dates, status.
- `knowledge_reviews`: reviewer, action, reason, timestamp, before/after content.

Indexes:

- `(organization_scope, status, created_at)` on gaps and candidates.
- `(organization_scope, canonical_topic, status)` on articles.
- `(candidate_id, relevance_score)` on evidence.
- Unique approved article version constraint per organization/topic/version.

Verification:

- Migration is idempotent.
- Tenant-scoped repository tests cannot retrieve another tenant’s rows.
- Review and publication states reject invalid transitions.

Rollback: drop only the new tables/indexes; existing memory and analytics remain unchanged.

## Step 2 — Create a separate human-agent evidence index

Dependencies: Step 1.

- Build bounded evidence windows containing the customer question, agent answer, relevant tool events, later corrections/retractions, and final disposition.
- Store metadata: organization, session, message ID, category, resolution state, timestamp, agent role, and source hash.
- Do not mix unapproved agent evidence with the bot’s approved knowledge index.
- Mask sensitive values before embedding where possible; retain secure source references for authorized reviewers.
- Add an ingestion/outbox job so database writes and vector indexing can retry safely.

Verification:

- Semantic paraphrases retrieve relevant agent answers.
- Cross-tenant searches return zero results.
- Unresolved, bot-authored, and deleted messages are excluded.
- Re-indexing the same source hash is idempotent.

Rollback: disable the evidence-index worker and delete its dedicated namespace only.

## Step 3 — Implement candidate research and evidence validation

Dependencies: Steps 1–2.

- Validate the gap belongs to the authenticated tenant.
- Normalize the gap into a canonical question and topic.
- Search the agent-evidence namespace with the exact question plus normalized topic.
- Retrieve 10–20 candidates, rerank, then retain a small evidence set.
- Prefer multiple independent resolved sessions over one answer.
- Detect contradictory answers, outdated answers, policy exceptions, and customer-specific answers.
- Classify evidence as policy claim, authoritative tool result, agent opinion, customer-reported outcome, or transactional result.
- Calculate transparent deterministic score components from relevance, source diversity, temporal validity, policy provenance, agreement, and claim-level citation coverage. LLM self-confidence never controls publication.

Suggested automatic-draft threshold: at least two independent agreeing resolved sessions and score ≥ 0.80. This creates a **suggested FAQ only**; agreement is not authoritative truth and does not permit publication.

Verification:

- Known answer, no-answer, conflict, stale-answer, and single-source fixtures.
- Returned evidence includes stable source IDs and scores.
- No customer identifiers appear in the candidate prompt/output.

Rollback: disable candidate generation; gaps continue to appear in analytics.

## Step 4 — Add grounded LLM synthesis

Dependencies: Step 3.

- Prompt the LLM with the question and selected evidence only.
- Require structured JSON: answer, supported claims, citations, conflicts, missing details, confidence rationale.
- Reject output containing claims without evidence citations.
- Verify each material claim is entailed by a quoted source span; a citation’s existence alone is insufficient.
- Reject transactional promises and medical/financial conclusions unless backed by an authoritative tool/policy source.
- Store model name, prompt version, evidence hashes, and raw structured result for audit.

Verification:

- Hallucination test: LLM must say evidence is insufficient.
- Conflict test: LLM must return `conflicting_evidence`.
- Prompt-injection text inside agent chats is treated as quoted evidence, never as instruction.
- Output schema validation and timeout/fallback behavior.

Rollback: retain evidence search and present sources to reviewers without an LLM draft.

## Step 5 — Add review and publication workflow

Dependencies: Steps 1 and 4.

- Add an admin review queue grouped by topic and frequency.
- Reviewer can inspect cited chats, edit the answer, approve, reject, or request more evidence.
- Approval creates a versioned article in a tenant-specific approved-knowledge namespace.
- Editing/reapproval creates a new version; never overwrite audit history.
- Do not use automatic expiration dates. Knowledge remains active until the organization edits, disables, deletes, or supersedes it.
- Require authoritative verification from an approved policy, operational tool result, or designated subject-matter owner before publication.
- Block clinical, financial, legal, eligibility, policy, and transactional claims from chat-only publication.
- Use an outbox state machine: `draft → approved_pending_index → active`. Bot retrieval requires the same active version in SQL and the vector projection.
- Handle retry, dead letter, reconciliation, revocation, index tombstones, and cache invalidation without deleting audit records.

Verification:

- Unauthorized users cannot review or publish.
- Every approved article has citations and an audit record.
- Every approved material claim has authoritative provenance, not merely agent-chat consensus.
- Rejected drafts are never returned to customers.
- Expired articles are excluded from bot retrieval.

Rollback: unpublish an article version; previous approved version can be restored.

### Organization-owned agent chat library and automatic activation

- Store every completed human-agent conversation in an organization-isolated chat library.
- Organization administrators can search, view, edit derived knowledge, disable it, delete it, or restore a previous version according to retention policy.
- Raw chat messages remain immutable audit evidence. An administrator “editing a chat answer” creates a corrected derived-knowledge version; it does not silently rewrite historical messages.
- Every chat completed through the verified human-agent portal is automatically recorded and approved by the application for bot retrieval.
- When the human agent closes the chat, an asynchronous job extracts reusable question/answer knowledge and automatically activates it.
- Auto-approval is the standard application behavior for verified agent-portal chats; it does not apply to imported, bot-only, abandoned, or unverified conversations.
- Eligible chats must have a completed/closed disposition, a verified human-agent role, no deletion flag, no compliance hold, and a reusable answer rather than customer-specific transactional data.
- The organization remains responsible for disabling or modifying automatically activated knowledge.
- Clinical diagnosis, legal advice, payment authorization, credentials, patient records, and customer-specific transaction results are never auto-activated as reusable knowledge.

Automatic path:

```text
Verified human-agent portal chat is closed
    ↓
Persist immutable chat and closed disposition
    ↓
Extract reusable question/answer and redact customer data
    ↓
Application auto-approval and safety checks
    ↓
Create article version 1
    ↓
active_pending_index → active
```

Edit path:

```text
Organization edits active answer
    ↓
Create new immutable article version
    ↓
Index new version atomically
    ↓
New edited version is automatically approved and becomes active
    ↓
Old version becomes superseded but remains in audit history
```

Delete/disable path:

```text
Organization disables or deletes knowledge
    ↓
SQL status becomes disabled/deleted
    ↓
Outbox emits vector tombstone and cache invalidation
    ↓
Bot can no longer retrieve it
```

## Step 6 — Integrate approved knowledge into bot answering

Dependencies: Step 5.

Retrieval order:

1. Check authoritative application tools/database for transactional facts.
2. Search approved tenant knowledge articles. PostgreSQL is authoritative; a separate vector index is only its retrieval projection.
3. Search customer-specific Mem0 history when the question references a prior conversation.
4. Let the LLM answer using retrieved evidence only.
5. If nothing reliable is found, apologize, create/update a knowledge gap, and escalate.

### Live-chat approved-answer fast path

Human review happens once when agent-chat evidence is converted into an approved knowledge article. It does not block every live conversation.

During live chat, the bot may use an agent-derived answer immediately only when all checks pass:

- `article.status == active`
- the article belongs to the authenticated tenant
- `approved_at` and `approved_by` are present
- the exact SQL article version matches the indexed vector version
- none of its source messages/sessions are deleted, edited, revoked, or under correction
- no newer version supersedes it
- required authoritative provenance is still valid
- retrieval score and applicability checks meet the configured threshold

Store a source-version fingerprint for every cited chat message. Historical raw messages are immutable; corrections create a new derived-knowledge version. If a cited source is deleted or access is revoked, emit an invalidation event immediately:

```text
active → suspended_source_changed → pending_re_review
```

The outbox worker then removes/tombstones the vector projection and invalidates caches. Until re-approved, live chat must not retrieve that article.

Recommended live response metadata for internal audit:

```json
{
  "answer_source": "approved_agent_knowledge",
  "article_id": "article-uuid",
  "article_version": 3,
  "approved_at": "2026-08-10T10:00:00Z",
  "retrieval_score": 0.91
}
```

This metadata is stored internally and does not need to be shown to the customer.

Do not search raw agent chats during a live customer response. Raw chats are only used offline to create reviewed knowledge.

Verification:

- Newly approved article answers a paraphrased question.
- Another tenant cannot retrieve it.
- Low-confidence/no-evidence query still escalates.
- Article citation/version appears in internal response metadata.
- An approved active article answers without waiting for a new human review.
- Editing or deleting one cited source makes the article unavailable on the next retrieval.
- Revocation removes the article from SQL eligibility, vector retrieval, and cache.
- Editing derived knowledge activates the newly indexed version and supersedes the previous version without losing audit history.
- Knowledge remains active indefinitely unless the organization changes its status or version.
- Every verified agent-portal chat receives an immutable record and an automatic approval audit event when closed.
- Only the latest active edited version is returned to the bot; superseded versions are never retrieved.

Rollback: feature flag returns bot to current Mem0 + existing knowledge behavior.

## Step 7 — Backfill, evaluation, and operational rollout

Dependencies: Steps 2–6.

- Start with a small stratified pilot per tenant; do not immediately process all 3,389 gaps.
- Establish labeled retrieval baselines and reviewer agreement, then progressively expand to the remaining gaps.
- Process highest-frequency topics first with rate/cost limits, cancellation, replayability, and per-run audit IDs.
- Deduplicate equivalent questions before agent-chat search.
- Build a labeled evaluation set: answerable from agent chats, unanswerable, conflicting, outdated, privacy-sensitive.
- Measure retrieval recall, citation precision, unsupported-claim rate, approval rate, and repeated-gap reduction.
- Roll out per organization behind feature flags.

Release gates:

- Zero cross-tenant leakage in security tests.
- Zero published uncited answers.
- Unsupported-claim rate below the agreed threshold.
- Reviewer approval workflow tested end to end.
- Rollback exercised successfully.
- Source deletion/correction cascades to candidate invalidation, article re-review, vector tombstones, and cache invalidation.

## Dependency graph and parallel work

```text
Step 0 ──→ Step 1 ──→ Step 2 ──→ Step 3 ──→ Step 4 ──→ Step 5 ──→ Step 6 ──→ Step 7
  └────────────────────────────────→ Step 5
```

After Step 1, review-UI scaffolding from Step 5 may proceed in parallel with Steps 2–4, but publication cannot complete until grounded synthesis exists.

## Anti-patterns to avoid

- Fine-tuning directly on raw agent chats.
- Treating agreement between multiple agents as authoritative truth.
- Automatically publishing the first matching agent response.
- Using Mem0 customer memories as global company policy.
- Mixing raw evidence and approved knowledge in one namespace.
- Treating a high vector score as proof that an answer is correct.
- Sending customer identifiers to an external LLM.
- Hiding conflicts or source citations from reviewers.
- Letting the browser provide tenant identity without authenticated server claims.
- Using LLM self-confidence as a publication gate.
- Keeping approved organizational knowledge only in Mem0 instead of a versioned system of record.

## Recommended first deliverable

A read-only `research_missing_topic` tool that accepts only `knowledge_gap_id`, derives tenant and question server-side, and returns redacted cited evidence plus an LLM draft with `pending_review`. It must not publish or answer customers automatically. The first rollout is a small stratified tenant pilot, not the complete historical dataset.
