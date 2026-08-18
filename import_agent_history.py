import json
import time
import urllib.request
import urllib.error
import os
import re
import traceback
from datetime import datetime
import sqlite3
import sys

# Ensure we can import analytics from current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from analytics import AnalyticsRepository

API_URL = "http://127.0.0.1:8765/api/memories"
HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": "quicktalk-local-demo-2026"
}

JSON_FILE = r"D:\najoomi\20july_1auguserData.agent_chat_history.json"
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "import_progress.txt")

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            content = f.read().strip()
            if content.isdigit():
                return int(content)
    return 0

def save_progress(index):
    with open(PROGRESS_FILE, 'w') as f:
        f.write(str(index))

def extract_mobile(session_id):
    # E.g. Shifa_International_03434878887 -> +923434878887
    parts = session_id.split('_')
    for part in reversed(parts):
        if part.isdigit() and len(part) >= 10:
            if part.startswith('0'):
                return '+92' + part[1:]
            return '+' + part
    # fallback
    digits = re.sub(r"\D", "", session_id)
    if digits:
        if digits.startswith('0'):
            return '+92' + digits[1:]
        return '+' + digits
    return "+920000000000"

def extract_mem0_fact(org_id, session_id, mobile_no, text):
    payload = {
        "organization_id": org_id,
        "session_id": session_id,
        "mobile_no": mobile_no,
        "text": text,
        "role": "system",
        "infer": True
    }
    
    max_retries = 10
    base_wait = 5
    
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(API_URL, data=json.dumps(payload).encode("utf-8"), headers=HEADERS, method="POST")
            with urllib.request.urlopen(req) as resp:
                if resp.status == 201:
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = base_wait * (2 ** attempt)
                print(f"Rate limited (429). Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"HTTP Error {e.code}: {e.reason}")
                return False
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(2)
            continue
    print(f"Failed to extract facts for {session_id} after {max_retries} retries.")
    return False

def run_import():
    print(f"Loading data from {JSON_FILE}...")
    with open(JSON_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    total = len(data)
    start_index = load_progress()
    print(f"Found {total} sessions. Resuming from index {start_index}.")
    
    analytics = AnalyticsRepository(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "analytics.db"))
    
    # Pre-register organization just in case
    analytics.register_organization(
        scope="Shifa_International",
        tenant_id="tenant_1",
        organization_id="Shifa_International",
        organization_name="Shifa International",
        industry="healthcare"
    )

    batch_size = 50
    events_batch = []
    
    for i in range(start_index, total):
        session = data[i]
        org_id = session.get("organization_id", "Shifa_International")
        session_id = session.get("session_id", f"session_{i}")
        mobile_no = extract_mobile(session_id)
        
        # 1. Prepare raw events for bulk insertion
        history = session.get("history", [])
        for msg in history:
            text = msg.get("message", "").strip()
            if not text:
                continue
                
            role = msg.get("role", "customer")
            if role == "user": role = "customer"
            
            timestamp = msg.get("timestamp")
            if not timestamp:
                timestamp = datetime.utcnow().isoformat() + "Z"
                
            event_id = f"{session_id}_{len(events_batch)}"
            
            events_batch.append({
                "id": event_id,
                "organization_id": org_id,
                "mobile_no": mobile_no,
                "session_id": session_id,
                "role": role,
                "text": text,
                "timestamp": timestamp
            })
            
        # Insert raw events if batch is large enough
        if len(events_batch) >= 1000:
            analytics.record_memories_bulk(events_batch)
            events_batch.clear()

        # 2. Extract Durable Facts via Mem0
        summary = session.get("chat_summary", "").strip()
        if summary and len(summary) > 5:
            success = extract_mem0_fact(org_id, session_id, mobile_no, summary)
            if success:
                print(f"[{i+1}/{total}] Extracted Mem0 facts for {mobile_no} | {summary[:50]}...")
            else:
                print(f"[{i+1}/{total}] Failed extraction for {mobile_no}")
        else:
            print(f"[{i+1}/{total}] No summary to extract for {mobile_no}")
            
        # Save progress every 10 sessions
        if i % 10 == 0:
            save_progress(i)
            
    # Insert any remaining events
    if events_batch:
        analytics.record_memories_bulk(events_batch)
        
    # Final progress save
    save_progress(total)
    
    print("\nTriggering Profile Analytics Recalculation...")
    analytics.backfill_profiles()
    print("All done! Import and bot training complete.")

if __name__ == "__main__":
    run_import()
