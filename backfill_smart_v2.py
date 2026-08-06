"""
Smart backfill v2: 
- Phase 1 (FAST): Load ALL JSON events into SQLite analytics with smart heuristic summaries (no Groq calls)
- Phase 2 (SMART): Use Groq LLM only for the LATEST session per customer (most important for welcome msg)
- Phase 3 (MEM0): Push latest-session LLM summaries to Mem0 (rate-limited but targeted)

This avoids blowing through Groq token limits on 382 sessions of old bulk data.
Run: python backfill_smart_v2.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
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
from core.sanitizer import clean_transcript_for_memory
from core.summarizer import generate_session_summary

SCRIPT_DIR = Path(__file__).parent

JSON_SOURCES = [
    (SCRIPT_DIR / "demo_data" / "three_month_large_analytics.json", "events_root"),
    (SCRIPT_DIR / "demo_data" / "healthcare_analytics.json",        "events_root"),
    (SCRIPT_DIR / "demo_data" / "two_month_tenant_history.json",    "nested_org"),
    (SCRIPT_DIR / "test_data" / "user_ahmed_ptcl.json",             "test_user"),
    (SCRIPT_DIR / "test_data" / "user_sadia_nayatel.json",          "test_user"),
    (SCRIPT_DIR / "test_data" / "user_zainab_stormfiber.json",      "test_user"),
]

# Groq calls: only for LATEST session per customer. Rate limit safe.
GROQ_DELAY_SECS = 4.0   # conservative delay between Groq summary calls
PUSH_TO_MEM0    = True   # push latest-session LLM summary to Mem0

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
    return raw.strip()


def extract_events(path: Path, fmt: str) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    events = []

    if fmt == "events_root":
        raw_events = data.get("events", data) if isinstance(data, dict) else data
        orgs_map = {}
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
        for e in raw_events:
            scope = e.get("organization_scope", top_scope) or "unknown--unknown"
            events.append({
                "organization_scope": scope,
                "mobile_no": normalize_mobile(e.get("mobile_no", "")),
                "session_id": e.get("session_id", "unknown"),
                "role": e.get("role", "customer"),
                "text": e.get("text", ""),
                "timestamp": e.get("timestamp", "2026-01-01T00:00:00+00:00"),
            })

    elif fmt == "nested_org":
        for org in data.get("organizations", []):
            scope = org.get("pinecone_scope", org.get("organization_scope", ""))
            if not scope:
                scope = f"{org.get('tenant_id','')}--{org.get('organization_id','')}"
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
                    for msg in session.get("messages", []):
                        events.append({
                            "organization_scope": scope,
                            "mobile_no": mobile,
                            "session_id": sid,
                            "role": msg.get("role", "customer"),
                            "text": msg.get("text", ""),
                            "timestamp": msg.get("timestamp", "2026-01-01T00:00:00+00:00"),
                        })

    elif fmt == "test_user":
        profile = data.get("user_profile", {})
        raw_org_id = profile.get("organization_id", "test")
        mobile = normalize_mobile(profile.get("mobile_no", ""))
        scope = f"test--{raw_org_id}"
        repo.register_organization(
            scope=scope, tenant_id="test", organization_id=raw_org_id,
            organization_name=profile.get("name", raw_org_id), industry="telecom",
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
            })
    return events


def heuristic_summary(events: list[dict]) -> str:
    """Fast heuristic summary — no LLM required."""
    customer = sorted([e for e in events if e["role"] != "assistant"], key=lambda x: x["timestamp"])
    assistant = sorted([e for e in events if e["role"] == "assistant"], key=lambda x: x["timestamp"])
    # Score customer messages — pick the most informative
    scored = []
    for e in customer:
        txt = e["text"].strip()
        if not txt or len(txt) < 5:
            continue
        score = len(txt)
        if re.search(r"\b(dr|doctor|appointment|book|fee|bill|payment|internet|plan|upgrade|speed|claim|insurance|transfer|fraud|kyc|charge|router|disconnect)\b", txt, re.I):
            score += 200
        scored.append((score, txt))
    scored.sort(reverse=True)
    issue = scored[0][1][:90] if scored else (customer[0]["text"][:90] if customer else "Customer enquiry")
    action = assistant[-1]["text"][:90] if assistant else "Support responded"
    outcome = customer[-1]["text"][:80] if len(customer) > 1 else "Pending"
    return f"Issue: {issue} Action: {action} Outcome: {outcome}"


def groq_summary(events: list[dict]) -> str:
    """Call Groq LLM for a structured CoT summary."""
    transcript_lines = [
        f"{'Customer' if e['role'] != 'assistant' else 'Support'}: {e['text']}"
        for e in sorted(events, key=lambda x: x["timestamp"])
    ]
    cleaned_lines = clean_transcript_for_memory(transcript_lines)
    transcript = "\n".join(cleaned_lines)
    
    result = generate_session_summary(transcript)
    if result and "issue" in result:
        return f"Issue: {result['issue']} | Action: {result['action']} | Outcome: {result['outcome']}"
    return heuristic_summary(events)


def main():
    print("=" * 65)
    print("QuickTalk Smart Backfill v2 - Groq LLM for Latest Sessions")
    print("=" * 65)

    # ── Phase 1: Load all JSON files ──────────────────────────────────
    all_events: list[dict] = []
    for path, fmt in JSON_SOURCES:
        if not path.exists():
            print(f"  [SKIP] {path.name}")
            continue
        try:
            evts = extract_events(path, fmt)
            all_events.extend(evts)
            print(f"  [LOAD] {path.name}: {len(evts)} events")
        except Exception as e:
            print(f"  [ERR ] {path.name}: {e}")

    # Group by session key
    sessions: dict[str, list[dict]] = defaultdict(list)
    for e in all_events:
        key = "__".join([e["organization_scope"], e["mobile_no"], e["session_id"]])
        sessions[key].append(e)

    # Group by customer key → find latest session per customer
    customer_sessions: dict[str, list[str]] = defaultdict(list)
    for key in sessions:
        parts = key.split("__", 2)
        if len(parts) == 3:
            cust_key = f"{parts[0]}__{parts[1]}"
            customer_sessions[cust_key].append(key)

    # Find the latest session key per customer (by latest timestamp in session)
    latest_session_per_customer: dict[str, str] = {}
    for cust_key, sess_keys in customer_sessions.items():
        def latest_ts(k):
            evts = sessions[k]
            return max(e["timestamp"] for e in evts) if evts else "0"
        latest_session_per_customer[cust_key] = max(sess_keys, key=latest_ts)

    latest_session_keys = set(latest_session_per_customer.values())

    total_sessions = len(sessions)
    total_customers = len(customer_sessions)
    groq_calls = len(latest_session_keys)

    print(f"\nTotal events : {len(all_events)}")
    print(f"Total sessions: {total_sessions}")
    print(f"Total customers: {total_customers}")
    print(f"Groq LLM calls: {groq_calls} (latest session per customer only)")
    print(f"Heuristic summaries: {total_sessions - groq_calls} (older sessions)")
    print(f"Estimated time: ~{(groq_calls * GROQ_DELAY_SECS) / 60:.1f} min")
    print()

    # ── Phase 2: Bulk insert ALL events into SQLite (fast, no LLM) ────
    print("[Phase 1/3] Bulk-inserting all events into SQLite analytics...")
    def all_records():
        for key, evts in sessions.items():
            for e in evts:
                yield {
                    "id": str(uuid.uuid4()),
                    "organization_id": e["organization_scope"],
                    "mobile_no": e["mobile_no"],
                    "session_id": e["session_id"],
                    "role": e["role"],
                    "text": e["text"],
                    "timestamp": e["timestamp"],
                }
    try:
        inserted = repo.record_memories_bulk(all_records())
        print(f"  Inserted {inserted} records into SQLite.")
    except Exception as err:
        print(f"  [WARN] Bulk insert error: {err}")

    # ── Phase 3: Generate summaries + push to Mem0 ────────────────────
    print(f"\n[Phase 2/3] Generating summaries for all {total_sessions} sessions...")
    stats = {"groq": 0, "heuristic": 0, "mem0_ok": 0, "mem0_fail": 0, "errors": 0}

    # Setup Mem0 store
    mem0_store = None
    if PUSH_TO_MEM0:
        try:
            from mem0_memory import create_memory_store
            mem0_store = create_memory_store()
            print(f"  [Mem0] Connected: {type(mem0_store).__name__}")
        except Exception as e:
            print(f"  [Mem0] WARNING: {e}")

    for idx, (key, evts) in enumerate(sessions.items(), 1):
        parts = key.split("__", 2)
        scope, mobile, session_id = parts if len(parts) == 3 else (key, "", "")
        cust_key = f"{scope}__{mobile}"
        is_latest = (key == latest_session_per_customer.get(cust_key, ""))

        if is_latest:
            # Use Groq LLM for latest session
            try:
                summary = groq_summary(evts)
                stats["groq"] += 1
                flag = "[GROQ]"
                time.sleep(GROQ_DELAY_SECS)
            except Exception as e:
                summary = heuristic_summary(evts)
                stats["heuristic"] += 1
                stats["errors"] += 1
                flag = "[HEUR]"
        else:
            # Use fast heuristic for older sessions
            summary = heuristic_summary(evts)
            stats["heuristic"] += 1
            flag = "[HEUR]"

        # Progress every 20 sessions or when using Groq
        if idx % 20 == 0 or is_latest:
            print(f"  [{idx:>4}/{total_sessions}] {flag} {mobile} | {session_id[:30]:<30} | {summary[:70]}")

        # Push ONLY latest-session summaries to Mem0
        if is_latest and mem0_store:
            try:
                mem0_store.add(
                    organization_id=scope,
                    session_id=session_id,
                    mobile_no=mobile,
                    text=summary,
                    role="customer",
                    infer=True,
                )
                stats["mem0_ok"] += 1
            except Exception as err:
                stats["mem0_fail"] += 1
                print(f"         [Mem0 FAIL] {mobile}: {err}")

    # ── Phase 4: Rebuild all customer profiles ────────────────────────
    print(f"\n[Phase 3/3] Rebuilding customer profiles in SQLite...")
    rebuilt = 0
    for cust_key in customer_sessions:
        sc, mob = cust_key.split("__", 1)
        try:
            repo._recompute_profile(sc, mob)
            rebuilt += 1
        except Exception as e:
            pass  # silently skip
    print(f"  Rebuilt {rebuilt} customer profiles.")

    # ── Final Report ─────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("BACKFILL COMPLETE")
    print(f"  Sessions processed    : {total_sessions}")
    print(f"  Groq LLM summaries    : {stats['groq']}")
    print(f"  Heuristic summaries   : {stats['heuristic']}")
    print(f"  Mem0 memories pushed  : {stats['mem0_ok']}")
    print(f"  Mem0 failures         : {stats['mem0_fail']}")
    print(f"  Errors                : {stats['errors']}")
    print("=" * 65)
    print()
    print("Open http://127.0.0.1:8765/custom to see your updated profiles!")


if __name__ == "__main__":
    main()
