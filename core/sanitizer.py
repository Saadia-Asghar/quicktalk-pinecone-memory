"""Filters transcripts to remove temporary system noise before memory extraction."""

from __future__ import annotations

def clean_transcript_for_memory(transcript_lines: list[str]) -> list[str]:
    """Filters out dead inputs, system crashes, and repetitive hold messages."""
    noise_signatures = [
        "Please wait while we connect you",
        "Can't process because of technical issue",
        "Welcome! How can I assist",
        "Thank you for contacting us"
    ]
    
    sanitized = []
    for line in transcript_lines:
        # Check against known noise
        if any(noise.lower() in line.lower() for noise in noise_signatures):
            continue
            
        # Drop empty utterances or single characters
        parts = line.split(":", 1)
        content = parts[1].strip() if len(parts) > 1 else line.strip()
        
        if len(content) < 3:
            continue
            
        sanitized.append(line)
        
    return sanitized
