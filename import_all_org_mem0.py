"""Resumable Groq -> Mem0/Pinecone backfill for every organization."""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)
SOURCE = Path(os.environ["AGENT_HISTORY_JSON"]) if os.getenv("AGENT_HISTORY_JSON") else None
PROGRESS = ROOT / "mem0_all_org_progress.txt"
ERROR = ROOT / "mem0_all_org_error.txt"
MEMORY_URL = "http://127.0.0.1:8765/api/memories"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.getenv("GROQ_BACKFILL_MODEL", "openai/gpt-oss-20b")
BATCH_SIZE = int(os.getenv("MEM0_BACKFILL_BATCH_SIZE", "8"))
WRITE_WORKERS = int(os.getenv("MEM0_BACKFILL_WRITE_WORKERS", "1"))

def read_progress() -> int:
    try:
        return int(PROGRESS.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0

def checkpoint(value: int) -> None:
    PROGRESS.write_text(str(value), encoding="utf-8")

def mobile_from_session(session_id: str) -> str:
    for part in reversed(session_id.split("_")):
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 10:
            return "+92" + digits[1:] if digits.startswith("0") else "+" + digits
    digest = int(hashlib.sha256(session_id.encode()).hexdigest()[:12], 16) % 10_000_000_000
    return f"+99{digest:010d}"

def request_json(url: str, payload: dict, headers: dict, attempts: int = 8) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error = "unknown error"
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:500]}"
            if error.code not in (429, 500, 502, 503, 504):
                break
        except (TimeoutError, urllib.error.URLError) as error:
            last_error = str(error)
        time.sleep(min(3 * (2 ** attempt), 90))
    raise RuntimeError(last_error)

def groq_memories(batch: list[tuple[int, dict]]) -> dict[int, str]:
    groq_key = os.getenv("GROQ_MEMORY_API_KEY") or os.getenv("GROQ_API_KEY")
    if not groq_key:
        raise RuntimeError("GROQ_MEMORY_API_KEY or GROQ_API_KEY is required")
    entries = [{"index": index, "summary": str(session.get("chat_summary") or "")[:1800]}
               for index, session in batch]
    prompt = (
        "Convert each support chat summary into one concise durable customer memory. "
        "Keep preferences, requested service, appointment facts, unresolved issues, and outcomes. "
        "Remove greetings. Do not invent facts. Preserve the input language. Return JSON only as "
        "{\"items\":[{\"index\":0,\"memory\":\"...\"}]}.\n" +
        json.dumps(entries, ensure_ascii=False)
    )
    response = request_json(
        GROQ_URL,
        {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
         "temperature": 0, "max_completion_tokens": 1800,
         "response_format": {"type": "json_object"}},
        {"Content-Type": "application/json", "Authorization": f"Bearer {groq_key}",
         "User-Agent": "groq-python/1.0 quicktalk-backfill"},
    )
    parsed = json.loads(response["choices"][0]["message"]["content"])
    return {int(item["index"]): str(item["memory"]).strip()
            for item in parsed.get("items", []) if item.get("memory")}

def store_memory(index: int, session: dict, memory: str) -> None:
    organization = str(session.get("organization_id") or "Shifa_International")
    session_id = str(session.get("session_id") or f"historical-{index}")
    saved = request_json(
        MEMORY_URL,
        {"organization_id": organization, "session_id": session_id,
         "mobile_no": mobile_from_session(session_id), "text": memory,
         "role": "customer", "infer": False},
        {"Content-Type": "application/json", "X-API-Key": os.getenv("SERVICE_API_KEY", ""),
         "X-Organization-Scope": organization},
    )
    facts = saved.get("extracted_facts") or []
    if not any(item.get("id") and item.get("memory") for item in facts):
        raise RuntimeError(f"Mem0 did not confirm storage for source index {index}")

def run() -> None:
    if SOURCE is None:
        raise RuntimeError("AGENT_HISTORY_JSON must point to the agent history JSON file")
    sessions = json.loads(SOURCE.read_text(encoding="utf-8"))
    cursor = read_progress()
    ERROR.unlink(missing_ok=True)
    print(f"All-organization backfill starting at {cursor}/{len(sessions)}", flush=True)
    while cursor < len(sessions):
        end = min(cursor + BATCH_SIZE, len(sessions))
        selected = [(index, sessions[index]) for index in range(cursor, end)
                    if str(sessions[index].get("chat_summary") or "").strip()]
        try:
            extracted = groq_memories(selected) if selected else {}
            writes = []
            for index in range(cursor, end):
                session = sessions[index]
                summary = str(session.get("chat_summary") or "").strip()
                memory = extracted.get(index) or summary[:1800]
                if memory:
                    writes.append((index, session, memory))
            with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as executor:
                futures = [executor.submit(store_memory, *write) for write in writes]
                for future in futures:
                    future.result()
            checkpoint(end)
            cursor = end
            ERROR.unlink(missing_ok=True)
            print(f"Processed {cursor}/{len(sessions)}", flush=True)
            time.sleep(2.1)
        except Exception as error:
            ERROR.write_text(f"index={cursor}\n{type(error).__name__}: {error}", encoding="utf-8")
            raise
    print("All organizations fully stored in Mem0/Pinecone.", flush=True)

if __name__ == "__main__":
    run()
