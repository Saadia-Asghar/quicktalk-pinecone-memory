# Architecture and request flow

The application is a Flask service. Bots, LLM agents, and the Human Agent Inbox
all call the same HTTP API. The service can use direct Pinecone or Mem0 OSS over
Pinecone without changing callers.

## Components

```mermaid
flowchart LR
  A[Bot or LLM agent] -->|GET tool schemas / invoke tool| F[Flask API]
  U[Human Agent Inbox] -->|context-card request| F
  F --> R[Tool Registry]
  R --> B{MEMORY_BACKEND}
  B -->|pinecone| D[Direct adapter]
  B -->|mem0| M[Mem0 extraction and lifecycle]
  B -->|mem0-local, free| L[Ollama + embedded Qdrant]
  D --> P[(Pinecone)]
  D -->|no API key, development only| J[(Local JSON)]
  M --> O[OpenAI LLM and embeddings]
  M --> P
  L --> Q[(Local Qdrant)]
  P --> N[Namespace per organization]
```

## Tool-calling flow

```mermaid
sequenceDiagram
  participant Agent as LLM Agent
  participant Flask as Flask Tool API
  participant Store as Memory Adapter
  participant Vector as Pinecone / Mem0
  Agent->>Flask: GET /api/tools
  Flask-->>Agent: OpenAI-compatible function schemas
  Agent->>Flask: POST /api/tools/save_customer_memory/invoke
  Flask->>Store: validated arguments
  Store->>Vector: namespace + customer/session metadata
  Vector-->>Store: saved memory
  Store-->>Flask: normalized result
  Flask-->>Agent: tool result JSON
  Agent->>Flask: POST /api/tools/get_handoff_context/invoke
  Flask->>Vector: isolated customer recall
  Flask-->>Agent: exactly three history bullets
```

## Isolation model

1. `organization_id` becomes a Pinecone namespace.
2. `mobile_no` is normalized to a stable `+<digits>` identity.
3. Direct Pinecone searches always include the mobile metadata filter.
4. Mem0 hashes `organization_id:mobile_no` into an opaque `user_id` and also
   uses the organization namespace.
5. Optional `session_id` filters narrow recall to one conversation.

## Flask tool endpoints

- `GET /api/tools` returns function schemas usable by an LLM tool loop.
- `POST /api/tools/save_customer_memory/invoke` writes memory.
- `POST /api/tools/search_customer_memory/invoke` retrieves relevant memory.
- `POST /api/tools/get_handoff_context/invoke` creates the three-bullet card.

The caller remains responsible for its model loop: send the schemas to the LLM,
execute the selected Flask endpoint, then return the JSON tool result to the LLM.
This keeps model vendors outside the core service and makes the tool API usable
from OpenAI, Anthropic, or an internal orchestration layer.

## Request security

When `SERVICE_API_KEY` is configured, every memory and tool request must include
`X-API-Key`. Flask compares it in constant time. In production, terminate TLS at
the load balancer/API gateway and use the organization's identity and rate-limit
policies in addition to this service-level key.
