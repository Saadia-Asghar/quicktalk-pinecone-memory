import json
import time
import urllib.request
import urllib.error
import os

API_URL = "http://127.0.0.1:8765/api/agent-chats"
HEADERS = {
    "Content-Type": "application/json",
    "X-API-Key": os.getenv("SERVICE_API_KEY", ""),
    "X-Organization-Scope": "Shifa_International",
    "X-User-Role": "organization_admin",
    "X-User-ID": "script-importer"
}

JSON_FILE = os.getenv("AGENT_HISTORY_JSON", "")
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_import_progress.txt")

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

def call_api(url, org_id="Shifa_International", method="POST", payload=None):
    max_retries = 10
    base_wait = 5
    data = json.dumps(payload).encode("utf-8") if payload else None

    headers = HEADERS.copy()
    headers["X-Organization-Scope"] = org_id

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req) as resp:
                if resp.status in (200, 201):
                    response_body = resp.read().decode('utf-8')
                    return json.loads(response_body) if response_body else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = base_wait * (2 ** attempt)
                print(f"Rate limited (429). Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"HTTP Error {e.code}: {e.reason} for {url}")
                return None
        except Exception as e:
            print(f"Error: {e} for {url}")
            time.sleep(2)
            continue
    return None

def process_knowledge():
    if not JSON_FILE:
        raise RuntimeError("AGENT_HISTORY_JSON must point to the agent history JSON file")
    print(f"Loading data from {JSON_FILE}...")
    with open(JSON_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    total = len(data)
    start_index = load_progress()
    print(f"Found {total} sessions. Resuming knowledge import from index {start_index}.")

    for i in range(start_index, total):
        session = data[i]
        org_id = session.get("organization_id", "Shifa_International")
        mobile_no = session.get("session_id", f"user_{i}")

        history = session.get("history", [])
        if not isinstance(history, list):
            continue

        # Check if an agent responded in this session
        has_agent = False
        for msg in history:
            if isinstance(msg, dict) and msg.get("role") in ["agent", "human"]:
                has_agent = True
                break

        if not has_agent:
            # Skip this session, no human escalation occurred
            if i % 50 == 0:
                save_progress(i)
            continue

        print(f"[{i+1}/{total}] Processing escalated session for {mobile_no}...")

        # 1. Create a simulated agent chat session in the backend
        session_data = call_api(API_URL, org_id=org_id, method="POST", payload={
            "organization_id": org_id,
            "customer_id": mobile_no,
            "agent_id": "historical-import-agent"
        })

        if not session_data or "id" not in session_data:
            print(f"Failed to create backend session for {mobile_no}. Skipping.")
            continue

        server_session_id = session_data["id"]

        # 2. Inject all messages (both customer and agent)
        valid_messages = 0
        for msg in history:
            if not isinstance(msg, dict):
                continue
            text = msg.get("message", "")
            if not isinstance(text, str):
                text = str(text)
            text = text.strip()

            if not text:
                continue

            role = msg.get("role", "customer")
            if role == "user": role = "customer"
            if role == "human": role = "agent"

            call_api(f"{API_URL}/{server_session_id}/messages", org_id=org_id, method="POST", payload={
                "organization_id": org_id,
                "sender_role": role,
                "text": text
            })
            valid_messages += 1

        # 3. Close the session to trigger auto-indexing (LLM extraction -> Mem0)
        if valid_messages >= 2:
            print(f"   Pushing {valid_messages} messages through LLM extraction for Mem0...")
            result = call_api(f"{API_URL}/{server_session_id}/close", org_id=org_id, method="POST", payload={
                "organization_id": org_id
            })
            if result and result.get("article"):
                print("   [Success] Knowledge Article created and pushed to Mem0!")
            else:
                print("   [Skip] LLM could not extract a reusable Q&A from this conversation.")

        # Save progress every time we successfully process an escalated session
        save_progress(i)

    save_progress(total)
    print("Knowledge import complete!")

if __name__ == "__main__":
    process_knowledge()
