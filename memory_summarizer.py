"""Local, privacy-preserving summaries and contextual customer greetings."""

from __future__ import annotations

import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def summarize_profile(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "New customer; no conversation history is available from the last 30 days."
    prompt = (
        "Summarize this customer's last 30 days for a human support agent in 3 concise "
        "sentences. State the latest issue, resolution status, sentiment, and useful prior context. "
        "Do not invent facts.\n\n" + _memory_text(memories)
    )
    return _ollama(prompt) or _fallback_summary(memories)


def clean_memory_text(text: str) -> str:
    """Extract the core issue text from a session summary string."""
    match = re.search(r"Issue:\s*(.*?)(?=\s*(?:Action:|Outcome:|$))", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def contextual_welcome(memories: list[dict[str, Any]]) -> str:
    customer_memories = [item for item in memories if item.get("role") != "assistant"]
    if not customer_memories:
        return "Hello! How can I help you today?"
        
    from analytics import is_greeting
    
    latest_meaningful = None
    for item in customer_memories:
        txt = clean_memory_text(str(item.get("text", "")))
        if not is_greeting(txt):
            latest_meaningful = txt
            break
            
    if not latest_meaningful:
        return "Hello! How can I help you today?"
        
    prompt = (
        "Write one friendly customer-support welcome sentence based only on the latest memory. "
        "Ask whether the previous issue is resolved or offer to continue helping. Do not mention "
        "databases, memory, or internal systems. Maximum 25 words.\n\nLatest memory: "
        + latest_meaningful
    )
    generated = _ollama(prompt)
    if generated:
        quoted = re.findall(r'"([^"\n]+)"', generated)
        return (quoted[-1] if quoted else generated).strip()
    return _fallback_welcome(latest_meaningful)


def summarize_sessions(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one agent-friendly summary for each conversation session."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in memories:
        session_id = str(item.get("session_id") or "unknown-session")
        grouped.setdefault(session_id, []).append(item)

    ordered = sorted(
        grouped.items(),
        key=lambda pair: max(str(item.get("timestamp", "")) for item in pair[1]),
        reverse=True,
    )
    with ThreadPoolExecutor(max_workers=min(len(ordered), 4) or 1) as executor:
        summaries = list(executor.map(lambda pair: _summarize_session(*pair), ordered))
    return summaries


def _summarize_session(session_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    chronological = sorted(items, key=lambda item: str(item.get("timestamp", "")))
    transcript = " ".join(str(item.get("text", "")) for item in chronological)
    explicitly_resolved = bool(
        re.search(r"\b(resolved|fixed|restored|received|completed|active now)\b", transcript, re.I)
    )
    customer_items = [item for item in chronological if item.get("role") != "assistant"]
    assistant_items = [item for item in chronological if item.get("role") == "assistant"]
    issue = str((customer_items or chronological)[0].get("text", ""))[:170]
    action = str(assistant_items[-1].get("text", ""))[:150] if assistant_items else ""
    outcome = str(customer_items[-1].get("text", ""))[:150] if len(customer_items) > 1 else ""
    parts = [f"Customer reported: {issue}"]
    if action:
        parts.append(f"Support action: {action}")
    if outcome and outcome != issue:
        parts.append(f"Latest outcome: {outcome}")
    parts.append("Resolved." if explicitly_resolved else "Resolution is not recorded.")
    summary = " ".join(parts)
    return {
        "session_id": session_id,
        "started_at": chronological[0].get("timestamp", ""),
        "ended_at": chronological[-1].get("timestamp", ""),
        "message_count": len(chronological),
        "summary": summary or "No session details are available.",
        "resolution_status": "resolved" if explicitly_resolved else "not_recorded",
    }


def _ollama(prompt: str) -> str | None:
    if os.getenv("OLLAMA_SUMMARIZER_ENABLED", "false").lower() != "true":
        return None
    payload = json.dumps(
        {
            "model": os.getenv("OLLAMA_SUMMARIZER_MODEL", "llama3.2:1b"),
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_predict": int(os.getenv("OLLAMA_SUMMARIZER_MAX_TOKENS", "120")),
            },
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=float(os.getenv("OLLAMA_SUMMARIZER_TIMEOUT", "30"))
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
        text = str(result.get("response", "")).strip()
        return text or None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _memory_text(memories: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {item.get('timestamp', '')} [{item.get('role', 'memory')}]: {item.get('text', '')}"
        for item in reversed(memories)
    )


def _fallback_summary(memories: list[dict[str, Any]]) -> str:
    latest = str(memories[0].get("text", "No recent issue recorded."))
    sessions = len({item.get("session_id") for item in memories if item.get("session_id")})
    return f"Latest customer context: {latest} There are {sessions} session(s) in the last 30 days."


def _fallback_welcome(latest: str) -> str:
    from analytics import is_greeting
    short = clean_memory_text(latest).rstrip(".!?")[:100]
    if is_greeting(short):
        return "Hello! Has your issue been resolved, or can I help you further today?"
    return f"Hello! I see your last query was regarding \"{short}\". Has this been resolved, or can I help you further today?"
