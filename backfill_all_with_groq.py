"""
Full backfill script: reads ALL JSON data files, generates Groq LLM session summaries,
saves to SQLite analytics, and pushes to Mem0 for dev testing.

Run: python backfill_all_with_groq.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import re
from collections import defaultdict
from pathlib import Path

# ── Load .env ──────────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with _env_path.open(encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from analytics import AnalyticsRepository
from memory_summarizer import _ollama

# ── Config ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent

JSON_SOURCES = [
    # (json_file_path, format)
    # format: "events_root"   = {"events": [...]} flat or {"organizations": [...], "events": [...]}
    # format: "nested_org"    = {"organizations": [{customers: [{sessions: [{messages: [...]}]}]}]}
    # format: "test_user"     = {user_profile:{...}, save_payloads:[...]}
    (SCRIPT_DIR / "demo_data" / "three_month_large_analytics.json", "events_root"),
    (SCRIPT_DIR / "demo_data" / "healthcare_analytics.json",        "events_root"),
    (SCRIPT_DIR / "demo_data" / "two_month_tenant_history.json",    "nested_org"),
    (SCRIPT_DIR / "test_data" / "user_ahmed_ptcl.json",             "test_user"),
    (SCRIPT_DIR / "test_data" / "user_sadia_nayatel.json",          "test_user"),
    (SCRIPT_DIR / "test_data" / "user_zainab_stormfiber.json",      "test_user"),
]

# Optional: push to Mem0 as well (set False to only do SQLite for speed)
PUSH_TO_MEM0 = True

# Groq rate limit: be conservative (free tier = 30 req/min)
GROQ_DELAY_SECS = 2.5   # delay between Groq calls

# ── Mem0 setup ─────────────────────────────────────────────────────────────────
mem0_store = None
if PUSH_TO_MEM0:
    try:
        from mem0_memory import create_memory_store
        mem0_store = create_memory_store()
        print(f"[Mem0] Connected to store: {type(mem0_store).__name__}")
    except Exception as e:
        print(f"[Mem0] WARNING: Could not connect to Mem0 store: {e}")
        print("[Mem0] Continuing with SQLite-only backfill.")
        mem0_store = None

# ── Analytics DB ───────────────────────────────────────────────────────────────
repo = AnalyticsRepository()

# ── Helpers ───────────────────────────────────────────────────────────────────
def normalize_mobile(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10 and digits.startswith("3"):
        return "+92" + digits
    if len(digits) == 11 and digits.startswith("03"):
        return "+92" + digits[1:]
    if len(digits) >= 12 and digits.startswith("92"):
        return "+" + digits
    return raw.strip() if raw.strip().startswith("+") else raw.strip()


def extract_events_from_json(path: Path, fmt: str) -> list[dict]:
    """Parse a JSON file and return a flat list of normalized event dicts."""
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    events = []

    if fmt == "events_root":
        raw_events = data.get("events", data) if isinstance(data, dict) else data
        orgs_map = {}

        # Multi-org format: has top-level "organizations" list
        if isinstance(data, dict) and "organizations" in data:
            for org in data["organizations"]:
                s = org.get("organization_scope", "")
                orgs_map[s] = org
                parts = s.split("--", 1)
                repo.register_organization(
                    scope=s,
                    tenant_id=parts[0] if len(parts) > 1 else s,
                    organization_id=parts[1] if len(parts) > 1 else s,
                    organization_name=org.get("organization_name", s),
                    industry=org.get("industry", "general"),
                )

        # Single-org format: organization_scope at top level
        top_scope = data.get("organization_scope", "") if isinstance(data, dict) else ""
        if top_scope and top_scope not in orgs_map:
            parts = top_scope.split("--", 1)
            repo.register_organization(
                scope=top_scope,
                tenant_id=data.get("tenant_id", parts[0] if len(parts) > 1 else top_scope),
                organization_id=data.get("organization_id", parts[1] if len(parts) > 1 else top_scope),
                organization_name=data.get("organization_name", top_scope),
                industry=data.get("industry", "general"),
            )
            orgs_map[top_scope] = {"organization_scope": top_scope}

        for e in raw_events:
            # Use event-level scope, fall back to top-level scope
            scope = e.get("organization_scope", top_scope) or top_scope or "unknown--unknown"
            events.append({
                "organization_scope": scope,
                "mobile_no": normalize_mobile(e.get("mobile_no", "")),
                "session_id": e.get("session_id", "unknown-session"),
                "role": e.get("role", "customer"),
                "text": e.get("text", ""),
                "timestamp": e.get("timestamp", "2026-01-01T00:00:00+00:00"),
                "category": e.get("source_category", e.get("category", "General")),
                "resolution_status": e.get("expected_resolution", e.get("resolution_status", "not_recorded")),
            })

    elif fmt == "test_user":
        # Test user format: {user_profile:{...}, save_payloads:[{payload:{arguments:{...}}}]}
        profile = data.get("user_profile", {})
        raw_org_id = profile.get("organization_id", "test")
        mobile = normalize_mobile(profile.get("mobile_no", ""))
        scope = f"test--{raw_org_id}"
        repo.register_organization(
            scope=scope,
            tenant_id="test",
            organization_id=raw_org_id,
            organization_name=profile.get("name", raw_org_id),
            industry="telecom",
        )
        for item in data.get("save_payloads", []):
            args = (item.get("payload") or {}).get("arguments") or {}
            if not args.get("text"):
                continue
            events.append({
                "organization_scope": scope,
                "mobile_no": normalize_mobile(args.get("mobile_no", mobile)),
                "session_id": args.get("session_id", "session-1"),
                "role": args.get("role", "customer"),
                "text": args.get("text", ""),
                "timestamp": args.get("timestamp", "2026-01-01T00:00:00+00:00"),
                "category": args.get("category", "General"),
                "resolution_status": "not_recorded",
            })

    elif fmt == "nested_org":
        # Format: {organizations: [{tenant_id, organization_id, organization_name, pinecone_scope,
        #          customers: [{display_name, mobile_no, sessions: [{session_id, messages: [...]}]}]}]}
        for org in data.get("organizations", []):
            scope = org.get("pinecone_scope", org.get("organization_scope", ""))
            if not scope:
                tid = org.get("tenant_id", "")
                oid = org.get("organization_id", "")
                scope = f"{tid}--{oid}"
            parts = scope.split("--", 1)
            repo.register_organization(
                scope=scope,
                tenant_id=parts[0] if len(parts) > 1 else scope,
                organization_id=parts[1] if len(parts) > 1 else scope,
                organization_name=org.get("organization_name", scope),
                industry=org.get("industry", "telecom"),
            )
            for customer in org.get("customers", []):
                mobile = normalize_mobile(customer.get("mobile_no", ""))
                for session in customer.get("sessions", []):
                    sid = session.get("session_id", "session-1")
                    category = session.get("category", "General")
                    for msg in session.get("messages", []):
                        events.append({
                            "organization_scope": scope,
                            "mobile_no": mobile,
                            "session_id": sid,
                            "role": msg.get("role", "customer"),
                            "text": msg.get("text", ""),
                            "timestamp": msg.get("timestamp", "2026-01-01T00:00:00+00:00"),
                            "category": category,
                            "resolution_status": "not_recorded",
                        })

    return events


def generate_groq_summary(events: list[dict]) -> str:
    """Use Groq LLM to generate Issue/Action/Outcome summary for a session."""
    customer = [e for e in events if e["role"] != "assistant"]
    assistant = [e for e in events if e["role"] == "assistant"]

    transcript = "\n".join(
        f"{'Customer' if e['role'] != 'assistant' else 'Support'}: {e['text']}"
        for e in sorted(events, key=lambda x: x["timestamp"])
    )

    prompt = (
        "You are a contact center AI. Summarize this customer support chat in the EXACT format below.\n"
        "Format: Issue: <what the customer needed> Action: <what support did> Outcome: <final result>\n"
        "Rules:\n"
        "- Be SPECIFIC: include doctor names, fees, plan names, amounts if mentioned\n"
        "- Maximum 35 words total\n"
        "- Output ONLY the formatted line, nothing else\n\n"
        f"Chat:\n{transcript}\n\nSummary:"
    )

    result = _ollama(prompt)
    if result and "issue:" in result.lower():
        return result.strip().replace("\n", " ").replace("|", " ")

    # Smart heuristic fallback
    issue_txt = next((e["text"] for e in customer if len(e["text"]) > 5), "Customer enquiry")
    action_txt = assistant[-1]["text"] if assistant else "Support responded"
    outcome_txt = customer[-1]["text"] if len(customer) > 1 else "Pending"
    return f"Issue: {issue_txt[:80]} Action: {action_txt[:80]} Outcome: {outcome_txt[:60]}"


# ── Main Backfill ──────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("QuickTalk Full Backfill - Groq LLM Session Summaries")
    print("=" * 60)

    all_events: list[dict] = []

    for path, fmt in JSON_SOURCES:
        if not path.exists():
            print(f"  [SKIP] {path.name} - file not found")
            continue
        try:
            events = extract_events_from_json(path, fmt)
            all_events.extend(events)
            print(f"  [LOAD] {path.name}: {len(events)} events")
        except Exception as e:
            print(f"  [ERROR] {path.name}: {e}")

    print(f"\nTotal events loaded: {len(all_events)}")

    # ── Group by session ──────────────────────────────────────────────────────
    sessions: dict[str, list[dict]] = defaultdict(list)
    for e in all_events:
        key = f"{e['organization_scope']}|||{e['mobile_no']}|||{e['session_id']}"
        sessions[key].append(e)

    print(f"Total unique sessions: {len(sessions)}")
    print()

    stats = {"sessions": 0, "summaries_generated": 0, "mem0_pushed": 0, "errors": 0}

    for idx, (key, events) in enumerate(sessions.items(), 1):
        scope, mobile, session_id = key.split("|||")
        stats["sessions"] += 1

        print(f"[{idx:>4}/{len(sessions)}] {scope[:40]:<40} | {mobile} | {session_id[:30]}")

        # Step 1: Save all raw events to SQLite analytics using bulk insert
        import uuid
        def _make_records(evts):
            for e in evts:
                yield {
                    "id": str(uuid.uuid4()),
                    "organization_id": e["organization_scope"],  # analytics uses org_id = scope
                    "mobile_no": e["mobile_no"],
                    "session_id": e["session_id"],
                    "role": e["role"],
                    "text": e["text"],
                    "timestamp": e["timestamp"],
                }
        try:
            inserted = repo.record_memories_bulk(_make_records(events))
        except Exception as err:
            print(f"         [WARN] SQLite bulk insert failed: {err}")

        # Step 2: Generate Groq LLM session summary
        try:
            summary_text = generate_groq_summary(events)
            stats["summaries_generated"] += 1
            print(f"         Summary: {summary_text[:100]}")
            time.sleep(GROQ_DELAY_SECS)  # respect Groq rate limit
        except Exception as err:
            print(f"         [ERROR] Groq summary failed: {err}")
            stats["errors"] += 1
            summary_text = f"Issue: {events[0]['text'][:60]} Action: Recorded Outcome: Pending"

        # Step 3: Push summary to Mem0 as a durable memory
        if mem0_store and summary_text:
            try:
                mem0_store.add(
                    organization_id=scope,
                    session_id=session_id,
                    mobile_no=mobile,
                    text=summary_text,
                    role="customer",
                    infer=True,   # let Mem0 extract facts from the summary
                )
                stats["mem0_pushed"] += 1
                print(f"         [Mem0] Pushed OK")
            except Exception as err:
                print(f"         [Mem0] Push failed: {err}")

        # Step 4: Rebuild profile after each session
        try:
            repo._recompute_profile(scope, mobile)
        except Exception as err:
            print(f"         [WARN] Profile rebuild failed: {err}")

    # ── Final report ──────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("BACKFILL COMPLETE")
    print(f"  Sessions processed  : {stats['sessions']}")
    print(f"  LLM summaries made  : {stats['summaries_generated']}")
    print(f"  Mem0 memories pushed: {stats['mem0_pushed']}")
    print(f"  Errors              : {stats['errors']}")
    print("=" * 60)
    print()
    print("You can now open http://127.0.0.1:8765/custom to see profiles.")


if __name__ == "__main__":
    main()
