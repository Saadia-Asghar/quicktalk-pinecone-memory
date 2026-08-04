"""Test runner utility to execute JSON payloads against the memory service and verify tenant isolation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import urllib.request
import urllib.error
from dotenv import load_dotenv

load_dotenv()

def send_request(url: str, method: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    req_headers = {
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)
        
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            res_data = response.read().decode("utf-8")
            return response.status, json.loads(res_data) if res_data else {}
    except urllib.error.HTTPError as e:
        try:
            err_data = e.read().decode("utf-8")
            return e.code, json.loads(err_data) if err_data else {"error": e.reason}
        except Exception:
            return e.code, {"error": str(e)}
    except urllib.error.URLError as e:
        return 0, {"error": f"Failed to connect to server: {e.reason}"}

def run_tests(host: str, api_key: str | None):
    # Establish headers
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
        print(f"Using X-API-Key for authentication: {api_key[:4]}...{api_key[-4:] if len(api_key) > 8 else ''}")
    else:
        print("No API Key configured, executing without authentication headers.")

    # Check health
    health_url = f"{host}/api/health"
    print(f"\nChecking health of the memory service at {health_url}...")
    status, health_res = send_request(health_url, "GET")
    if status != 200 or health_res.get("status") != "ok":
        print(f"ERROR: Memory service health check failed (Status {status}): {health_res}")
        print("Please start the Flask service first (e.g., run `python custom_app.py` in another terminal).")
        sys.exit(1)
        
    print(f"Health status OK: {health_res}")

    test_data_dir = Path("test_data")
    json_files = list(test_data_dir.glob("*.json"))
    if not json_files:
        print(f"ERROR: No JSON test files found in {test_data_dir.resolve()}")
        sys.exit(1)

    print(f"\nFound {len(json_files)} test profile files. Commencing test execution...")

    # Load and execute profiles
    profiles_data = []
    for jf in json_files:
        with open(jf, "r", encoding="utf-8") as f:
            profiles_data.append((jf.name, json.load(f)))

    # Step 1: Save memories for all users
    print("\n--- STEP 1: Saving customer and assistant messages ---")
    for fname, data in profiles_data:
        profile = data["user_profile"]
        print(f"\nProcessing Tenant '{profile['organization_id']}' for customer '{profile['name']}' ({profile['mobile_no']}):")
        
        for idx, step in enumerate(data["save_payloads"], start=1):
            url = f"{host}{step['endpoint']}"
            method = step["method"]
            desc = step["description"]
            payload = step["payload"]
            
            print(f"  [{idx}] Saving message: '{payload['arguments']['text']}' ({payload['arguments']['role']})")
            status, res = send_request(url, method, payload, headers)
            if status == 200:
                print(f"      Success: Memory stored successfully.")
            else:
                print(f"      FAILED (Status {status}): {res}")

    # Step 2: Search memories for each user
    print("\n--- STEP 2: Performing Semantic Memory Search ---")
    for fname, data in profiles_data:
        profile = data["user_profile"]
        search_step = data["search_payload"]
        url = f"{host}{search_step['endpoint']}"
        method = search_step["method"]
        payload = search_step["payload"]
        
        print(f"\nSearching memory for customer '{profile['name']}' in Tenant '{profile['organization_id']}':")
        print(f"  Query: '{payload['arguments']['query']}'")
        status, res = send_request(url, method, payload, headers)
        if status == 200:
            count = res.get("result", {}).get("count", 0)
            items = res.get("result", {}).get("items", [])
            print(f"  Found {count} matching memories:")
            for item in items:
                role = item.get("role", "unknown")
                text = item.get("text", "")
                print(f"    - [{role}] {text}")
        else:
            print(f"  Search failed (Status {status}): {res}")

    # Step 3: Retrieve handoff context card (3 bullets)
    print("\n--- STEP 3: Retrieving Agent Handoff Context (3 Bullets) ---")
    for fname, data in profiles_data:
        profile = data["user_profile"]
        handoff_step = data["handoff_payload"]
        url = f"{host}{handoff_step['endpoint']}"
        method = handoff_step["method"]
        payload = handoff_step["payload"]
        
        print(f"\nRequesting handoff context card for '{profile['name']}' ({profile['mobile_no']}) in Tenant '{profile['organization_id']}':")
        status, res = send_request(url, method, payload, headers)
        if status == 200:
            result_obj = res.get("result", {})
            bullets = result_obj.get("history_summary", [])
            print(f"  Handoff Context Bullets (Exactly {len(bullets)}):")
            for bullet in bullets:
                print(f"    * {bullet}")
        else:
            print(f"  Handoff context retrieval failed (Status {status}): {res}")

    # Step 4: Verify Tenant Isolation (Security Validation)
    print("\n--- STEP 4: Verifying Tenant Isolation (Cross-Tenant Security Check) ---")
    
    # Isolation test cases
    # We try to search User A's queries under User B's tenant/organization ID.
    isolation_tests = [
        {
            "desc": "Search for Sadia's internet issues under 'ptcl' tenant",
            "org_id": "ptcl",
            "mobile_no": "+923331234567", # Sadia's number
            "query": "unresolved internet or connection issues"
        },
        {
            "desc": "Search for Ahmed's upgrade requests under 'stormfiber' tenant",
            "org_id": "stormfiber",
            "mobile_no": "+923009876543", # Ahmed's number
            "query": "package upgrade GPON 100 Mbps"
        },
        {
            "desc": "Search for Zainab's duplicate router refund under 'nayatel' tenant",
            "org_id": "nayatel",
            "mobile_no": "+923214567890", # Zainab's number
            "query": "double router charge billing refund"
        }
    ]

    for test in isolation_tests:
        print(f"\nRunning Security Check: {test['desc']}")
        print(f"  Target Org: '{test['org_id']}', Mobile No: '{test['mobile_no']}'")
        
        url = f"{host}/api/tools/search_customer_memory/invoke"
        payload = {
            "arguments": {
                "organization_id": test["org_id"],
                "mobile_no": test["mobile_no"],
                "query": test["query"],
                "limit": 5
            }
        }
        status, res = send_request(url, "POST", payload, headers)
        if status == 200:
            count = res.get("result", {}).get("count", 0)
            print(f"  Result Count: {count}")
            if count == 0:
                print("  [PASS] Tenant Isolation active: No memories leaked across organizational boundaries.")
            else:
                print("  [FAIL] Security Leak detected! Memories returned across tenant boundaries.")
        else:
            print(f"  Verification failed with server error (Status {status}): {res}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Execute customer memory JSON payloads.")
    parser.path = "run_json_payloads.py"
    parser.add_argument("--host", default="http://127.0.0.1:8765", help="Host of the Flask service")
    parser.add_argument("--api-key", default=os.getenv("SERVICE_API_KEY"), help="API Key for X-API-Key header authentication")
    args = parser.parse_args()
    
    run_tests(args.host, args.api_key)
