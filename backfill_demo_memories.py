"""
Targeted Backfill: Generate and store durable Mem0 memories for all demo customers.
Runs Groq LLM fact extraction on the latest session summary of each demo customer
and persists facts into local Qdrant/Mem0 store so they are instantly visible in the UI.
"""

import os
import json
import time
from pathlib import Path

# Load environment
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with _env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from analytics import AnalyticsRepository, normalize_mobile
from mem0_memory import create_memory_store

repo = AnalyticsRepository()
store = create_memory_store()
print(f"Loaded Memory Store: {type(store).__name__} (backend={store.backend})")

# Demo customer list (the ones shown in the UI)
DEMO_FILES = [
    Path(__file__).parent / "demo_data" / "healthcare_analytics.json",
    Path(__file__).parent / "demo_data" / "two_month_tenant_history.json",
    Path(__file__).parent / "test_data" / "user_ahmed_ptcl.json",
    Path(__file__).parent / "test_data" / "user_sadia_nayatel.json",
    Path(__file__).parent / "test_data" / "user_zainab_stormfiber.json",
]

# Find distinct (org, mobile) pairs in demo files
demo_customers = set()

for p in DEMO_FILES:
    if not p.exists():
        continue
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    
    if "organizations" in data and isinstance(data["organizations"], list):
        for org in data["organizations"]:
            scope = org.get("pinecone_scope", org.get("organization_scope", ""))
            for cust in org.get("customers", []):
                mob = normalize_mobile(cust.get("mobile_no", ""))
                if scope and mob:
                    demo_customers.add((scope, mob))
    elif isinstance(data, dict) and "events" in data:
        scope = data.get("organization_scope", "Shifa_International--Healthcare_App")
        for e in data["events"]:
            mob = normalize_mobile(e.get("mobile_no", ""))
            if scope and mob:
                demo_customers.add((scope, mob))
    elif "user_profile" in data:
        profile = data["user_profile"]
        raw_org = profile.get("organization_id", "test")
        scope = f"test--{raw_org}"
        mob = normalize_mobile(profile.get("mobile_no", ""))
        if scope and mob:
            demo_customers.add((scope, mob))

print(f"Found {len(demo_customers)} demo customer profiles to process for Mem0 extraction.")

success_count = 0
fail_count = 0

for idx, (scope, mobile) in enumerate(sorted(demo_customers), 1):
    try:
        profile = repo.get_profile(scope, mobile, session_limit=1)
        if not profile or not profile.get("session_summaries"):
            print(f"[{idx}/{len(demo_customers)}] Skip {mobile} - no session summaries")
            continue
            
        latest_sess = profile["session_summaries"][0]
        session_id = latest_sess["session_id"]
        summary = latest_sess["summary"]
        
        print(f"[{idx}/{len(demo_customers)}] Processing {mobile} ({session_id[:15]}...)...")
        
        # Add to Mem0 with infer=True (Groq fact extraction)
        store.add(
            organization_id=scope,
            session_id=session_id,
            mobile_no=mobile,
            text=summary,
            role="system",
            infer=True,
            metadata={"memory_type": "session_summary"}
        )
        success_count += 1
        time.sleep(2.0) # rate limit friendly delay
        
    except Exception as err:
        fail_count += 1
        print(f"[{idx}/{len(demo_customers)}] Error for {mobile}: {err}")

print("=" * 60)
print(f"MEM0 DEMO BACKFILL COMPLETE: {success_count} succeeded, {fail_count} failed.")
print("=" * 60)
