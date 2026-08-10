"""Local, privacy-preserving summaries and contextual customer greetings."""

from __future__ import annotations

import json
import os
import re
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def summarize_profile(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "New customer; no conversation history is available from the last 30 days."
    prompt = (
        "Summarize this customer's last 30 days for a human support agent in 3 concise "
        "sentences. State the latest issue, resolution status, sentiment, and useful prior context. "
        "Do not invent facts. Respond in English only.\n\n" + _memory_text(memories)
    )
    return _ollama(prompt) or _fallback_summary(memories)


def clean_memory_text(text: str) -> str:
    """Extract the core issue text from a session summary string."""
    match = re.search(r"Issue:\s*(.*?)(?=\s*(?:Action:|Outcome:|$))", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def contextual_welcome(memories: list[dict[str, Any]], latest_session_summary: str = "") -> str:
    """Generate a warm, context-aware welcome using the latest session summary if available."""
    from analytics import is_greeting

    # Prefer the structured LLM-generated session summary (Issue/Action/Outcome format)
    context_text = ""
    if latest_session_summary and "issue:" in latest_session_summary.lower():
        # Extract Issue portion for a focused welcome
        issue_match = re.search(r"issue:\s*(.*?)(?:\s+action:|\s*$)", latest_session_summary, re.I | re.DOTALL)
        context_text = issue_match.group(1).strip() if issue_match else clean_memory_text(latest_session_summary)
    
    # Fall back to raw memories if no structured summary
    if not context_text:
        customer_memories = [item for item in memories if item.get("role") != "assistant"]
        for item in customer_memories:
            txt = clean_memory_text(str(item.get("text", "")))
            if txt and not is_greeting(txt):
                context_text = txt
                break

    if not context_text:
        return "Hello! How can I help you today?"

    prompt = (
        "You are a warm, friendly customer support agent. Write ONE welcome sentence to greet a returning customer. "
        "Reference their previous issue naturally and ask if it has been resolved or if they need further help. "
        "Do NOT use hyphens or em-dashes. Do NOT mention databases, memory, or AI. Maximum 25 words. Respond in English only.\n\n"
        f"Customer's previous issue: {context_text}\n\nWelcome message:"
    )
    generated = _ollama(prompt)
    if generated:
        quoted = re.findall(r'"([^"\n]+)"', generated)
        return (quoted[-1] if quoted else generated).strip()
    return _fallback_welcome(context_text)


def answer_from_memories(query: str, memories: list[dict[str, Any]]) -> str:
    """Create a grounded reply from tenant-scoped semantic memory results."""
    useful = []
    seen = set()
    for item in memories:
        text = str(item.get("text") or item.get("memory") or "").strip()
        if not text or text.casefold() == query.strip().casefold() or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        useful.append({
            "timestamp": str(item.get("timestamp", "")),
            "session_id": str(item.get("session_id", "")),
            "role": str(item.get("role", "memory")),
            "text": text,
        })
    if not useful:
        return "I could not find that information in your previous conversations."

    context = "\n".join(
        f"[{item['timestamp']}] [{item['session_id']}] {item['role']}: {item['text']}"
        for item in useful[:10]
    )
    prompt = (
        "Answer the customer using only the retrieved conversation memories below. "
        "Resolve references such as 'previously', 'last time', or 'that appointment' from the memories. "
        "Never invent a doctor, date, token, ticket, payment, diagnosis, or resolution. "
        "If the memories do not contain the answer, say you could not find it. "
        "Be concise and mention that the information comes from a previous conversation when appropriate.\n\n"
        f"Customer question: {query}\n\nRetrieved memories:\n{context}\n\nAnswer:"
    )
    generated = _ollama(prompt)
    if generated:
        return generated.strip()
    return f"From your previous conversation: {useful[0]['text']}"

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
    # 1. Groq API Routing (Free-Tier Developer Plan)
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key and os.getenv("GROQ_SUMMARIZER_ENABLED", "false").lower() == "true":
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = json.dumps({
            "model": os.getenv("MEM0_GROQ_MODEL", "llama-3.3-70b-versatile"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": int(os.getenv("OLLAMA_SUMMARIZER_MAX_TOKENS", "120"))
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {groq_key}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            method="POST",
        )
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(request, timeout=10.0) as response:
                    result = json.loads(response.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return str(choices[0].get("message", {}).get("content", "")).strip()
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries - 1:
                    retry_after = 15.0
                    try:
                        err_body = json.loads(e.read().decode("utf-8"))
                        msg = err_body.get("error", {}).get("message", "")
                        # Matches "try again in X.XXs" or "retry in X.XXs"
                        match = re.search(r"(?:try\s+again|retry)\s+in\s+([\d\.]+)", msg, re.I)
                        if match:
                            retry_after = float(match.group(1)) + 2.0
                    except Exception:
                        pass
                    print(f"Groq 429 Rate Limit hit. Retrying in {retry_after:.2f}s...")
                    time.sleep(retry_after)
                elif 400 <= e.code < 500:
                    print(f"Warning: Groq API call failed (attempt {attempt+1}): {e}")
                    break
                else:
                    if attempt < max_retries - 1:
                        print(f"Warning: Groq API call failed (attempt {attempt+1}): {e}. Retrying...")
                        time.sleep(5.0)
                    else:
                        break
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"Warning: Groq API call failed (attempt {attempt+1}): {e}. Retrying...")
                    time.sleep(5.0)
                else:
                    print(f"Warning: Groq API call failed (attempt {attempt+1}): {e}")
                    break

    # 2. Gemini API Routing (Free-Tier Google AI Studio)
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if gemini_key and os.getenv("GEMINI_SUMMARIZER_ENABLED", "false").lower() == "true":
        model = os.getenv("MEM0_GEMINI_MODEL", "gemini-1.5-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": int(os.getenv("OLLAMA_SUMMARIZER_MAX_TOKENS", "120"))
            }
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=10.0) as response:
                    result = json.loads(response.read().decode("utf-8"))
                candidates = result.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return str(parts[0].get("text", "")).strip()
                break
            except Exception as e:
                if attempt < 2:
                    print(f"Warning: Gemini API call failed (attempt {attempt+1}): {e}. Retrying in 10s...")
                    time.sleep(10.0)
                else:
                    print(f"Warning: Gemini API call failed: {e}")

    # 3. Local Ollama Fallback
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
