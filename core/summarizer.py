"""JSON-based Chain-of-Thought (CoT) summarizer for multi-domain chats."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from typing import Any

PROMPT_TEMPLATE = """You are an elite Quality Assurance AI analyzing hospital contact center chat transcripts. 
Your objective is to extract the core clinical/administrative intent, agent actions, and final resolution using Chain-of-Thought reasoning.

### DETAILED INSTRUCTIONS (WHAT TO DO):
1. DO read the entire transcript from start to finish before drawing conclusions.
2. DO capture medical identifiers like MR Numbers, Doctor Names, and Clinic Departments.
3. DO translate all Roman Urdu medical queries into professional English.
4. DO output exactly ONE valid JSON object and nothing else.
5. DO use the "reasoning" key to analyze slot selection, doctor shifts, or system escalation steps.

### STRICT CONSTRAINTS (WHAT NOT TO DO):
1. DO NOT include markdown blocks (like ```json), introductory text, or conversational filler.
2. DO NOT hallucinate appointment confirmations if the doctor was unavailable or the chat dropped. Use "Failed" or "Pending".
3. DO NOT exceed 15 words for "issue", "action", or "outcome".

### EXPECTED JSON STRUCTURE:
{
  "reasoning": "<Step-by-step logic analyzing patient intent and resolution>",
  "issue": "<Max 15 words describing clinical/admin need>",
  "action": "<Max 15 words describing hospital support action>",
  "outcome": "<Max 15 words describing final appointment/lab status>"
}

### EXAMPLES (CHAIN-OF-THOUGHT IN ACTION):

Transcript (Complex Booking / Doctor Pivot):
Customer: Dr. Khawaja
Support: This doctor has no available dates.
Customer: Matiullah
Support: Kindly pick a date for Dr. Matiullah Khan
Customer: 07:45 PM
Support: Your appointment for MR# 20130815 with Matiullah Khan is booked for 07:45 PM on July 31.
Output:
{
  "reasoning": "The user's initial request for Dr. Khawaja failed due to availability. The user pivoted to Dr. Matiullah. The bot guided them through slot selection and successfully booked the appointment.",
  "issue": "Booking appointment (initially Dr. Khawaja, booked Dr. Matiullah)",
  "action": "Checked availability and registered MR#",
  "outcome": "Booked with Dr. Matiullah on July 31"
}

Transcript (Unsuccessful Booking / Walk-away):
Customer: I want to book appointment with Dr. Nabeel Muzaffar Syed
Support: This doctor has no available dates to book. Kindly provide the name of some other doctor.
Customer: Okay thanks
Output:
{
  "reasoning": "The user requested an appointment with Dr. Nabeel, but there were no available dates. The user declined to choose another doctor and ended the chat.",
  "issue": "Wants appointment with Dr. Nabeel Muzaffar Syed",
  "action": "Checked availability",
  "outcome": "Failed (No available dates)"
}

Transcript (System Failure):
Customer: Assalamualaikum
Support: Can't process because of technical issue at the moment. Please try again later.
Output:
{
  "reasoning": "The user greeted the bot, but the system experienced an outage and failed to process the requests.",
  "issue": "General inquiry",
  "action": "None due to system outage",
  "outcome": "Failed (Technical Issue)"
}

Transcript:
{transcript}

Output:"""


def generate_session_summary(transcript_text: str) -> dict[str, str]:
    """Generates a structured CoT summary from a cleaned transcript."""
    if not transcript_text.strip():
        return {
            "reasoning": "Empty transcript.",
            "issue": "No data",
            "action": "None",
            "outcome": "No data"
        }
        
    prompt = PROMPT_TEMPLATE.replace("{transcript}", transcript_text)
    
    # Force Groq API with JSON format
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        print("Warning: GROQ_API_KEY not found in environment.")
        return {"error": "Missing API key"}
        
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = json.dumps({
        "model": os.getenv("MEM0_GROQ_MODEL", "llama-3.1-8b-instant"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}  # Enforce JSON
    }).encode("utf-8")
    
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {groq_key}",
            "User-Agent": "QuickTalk-Memory-Agent"
        },
        method="POST",
    )
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(request, timeout=15.0) as response:
                result = json.loads(response.read().decode("utf-8"))
            
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                return json.loads(content)
                
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = 15.0
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                    msg = err_body.get("error", {}).get("message", "")
                    match = re.search(r"(?:try\s+again|retry)\s+in\s+([\d\.]+)", msg, re.I)
                    if match:
                        retry_after = float(match.group(1)) + 2.0
                except Exception:
                    pass
                print(f"Groq Rate Limit hit. Retrying in {retry_after:.2f}s...")
                time.sleep(retry_after)
            else:
                print(f"Groq API Error: {e}")
                break
        except Exception as e:
            print(f"Groq Request Failed: {e}")
            break
            
    return {
        "reasoning": "Failed to generate summary after retries.",
        "issue": "API Error",
        "action": "API Error",
        "outcome": "API Error"
    }
