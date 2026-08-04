# Requirements and negative-test report

## Original requirement coverage

| Requirement | Implementation | Status |
|---|---|---|
| Flask service | `flask_app.py` and Flask tool routes | Met |
| Pinecone database | Live `quicktalk-mem0-free` serverless index | Met |
| Organizational session ID | Required `organization_id` and `session_id` metadata | Met |
| Timestamp | Validated timezone-aware UTC timestamp | Met |
| Mobile number | Required and normalized to `+<digits>` | Met |
| Persistent memories | Mem0 + Ollama embeddings + Pinecone | Met |
| Flask tool calling | Three schemas under `GET /api/tools` | Met |
| Human Agent Inbox | Custom local inbox at `/custom` | Met |
| Three-bullet history summary | `get_handoff_context` always returns exactly three bullets | Met |
| Card at top of inbox | `Agent Handoff Context` card displayed beside/top of chat | Met |
| Organization isolation | Separate Pinecone namespace per organization | Met |
| Customer isolation | Hashed Mem0 user ID from organization + mobile | Met |
| Free operation | Pinecone free tier + local Ollama; no OpenAI calls | Met within free-tier limits |

## Negative tests

**Execution result on 2026-07-29: PASS**

- Complete automated suite: 27 passed, 0 failed
- Live Pinecone rightful-customer results: 1
- Live Pinecone wrong-organization results: 0
- Live Pinecone wrong-mobile results: 0
- Missing live tool arguments: HTTP 400
- API key printed or exposed: no

The local negative suite verifies:

1. Missing required fields return HTTP 400.
2. Invalid mobile numbers return HTTP 400.
3. Invalid roles return HTTP 400.
4. Timestamps without a timezone return HTTP 400.
5. Malformed JSON returns a JSON HTTP 400 response.
6. Unknown/destructive tool names return HTTP 404.
7. Non-object tool arguments return HTTP 400.
8. Incorrect service API keys return HTTP 401.
9. Organization B cannot retrieve Organization A's memory.
10. Customer B cannot retrieve Customer A's memory.
11. Session B cannot retrieve Session A when a session filter is used.
12. Empty history safely returns exactly three bullets.

The live Pinecone negative test writes a unique private marker, confirms the
correct customer can retrieve it, and then proves both a different organization
and a different mobile number receive zero results.

## Commands

```powershell
python -m unittest -v test_negative_cases.py
python negative_live_pinecone_test.py
```

## Two-minute demonstration

1. Open `http://127.0.0.1:8765/custom`.
2. Keep organization `custom-demo-org`, enter a mobile and session.
3. Send: `My billing issue is still unresolved`.
4. Show the confirmation that memory was saved.
5. Click **Escalate to human**.
6. Show the three history bullets in the Agent Handoff Context card.
7. Run `python negative_live_pinecone_test.py`.
8. Point out: rightful customer `1`, wrong organization `0`, wrong mobile `0`.

## Scope note

These tests prove the memory service, Flask tool contract, identity boundaries,
live Pinecone storage, and handoff behavior. Final integration with another
production application still requires mapping that application's real
organization, session, customer-message, and escalation events to these tools.
