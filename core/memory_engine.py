"""Mem0 integration using Pinecone namespaces and strict extraction prompts."""

from __future__ import annotations

import os
from mem0 import Memory

STRICT_MEMORY_EXTRACTION_PROMPT = """You are a highly selective memory extraction system for enterprise support logs. 
Your objective is to extract ONLY persistent user facts, entity IDs (such as MR Numbers, Account IDs, Order numbers), and explicit preferences.

STRICT CONSTRAINTS:
1. DO NOT extract greetings, polite remarks, or conversational filler.
2. DO NOT extract temporary system error states or automated agent prompts ("Please wait...").
3. Keep extracted facts concise (maximum 10 words).
4. If no permanent user entity or intent exists, return an empty set.
"""

def get_production_memory(organization_id: str) -> Memory:
    """Initializes Mem0 with Pinecone using secure tenant namespaces and custom prompts."""
    # Build namespace safe string (e.g. replacing colons or weird chars if any)
    namespace = organization_id.replace(":", "-").lower()
    
    dimensions = int(os.getenv("MEM0_EMBEDDING_DIMENSION", "1536"))
    config = {
        "llm": {
            "provider": "openai",
            "config": {"model": os.getenv("MEM0_LLM_MODEL", "gpt-4.1-mini")},
        },
        "embedder": {
            "provider": "openai",
            "config": {"model": os.getenv("MEM0_EMBEDDING_MODEL", "text-embedding-3-small")},
        },
        "vector_store": {
            "provider": "pinecone",
            "config": {
                "collection_name": os.getenv("MEM0_PINECONE_INDEX", "quicktalk-mem0"),
                "embedding_model_dims": dimensions,
                "namespace": namespace,  # CRITICAL: Isolates client data
                "serverless_config": {
                    "cloud": os.getenv("PINECONE_CLOUD", "aws"),
                    "region": os.getenv("PINECONE_REGION", "us-east-1"),
                },
                "metric": "cosine"
            }
        },
        "custom_prompt": STRICT_MEMORY_EXTRACTION_PROMPT
    }
    
    return Memory.from_config(config)

def search_memories(memory: Memory, user_id: str, query: str, limit: int = 3, score_threshold: float = 0.75) -> list[dict]:
    """Search memories with hard-capped top-K filtering to reduce context noise."""
    results = memory.search(query=query, user_id=user_id, limit=limit)
    
    # Filter by pseudo-score if Mem0 returns it, otherwise just return top limit
    valid_results = []
    
    # Depending on mem0 version, search returns a list of dicts or a dict with 'results'
    items = results.get("results", []) if isinstance(results, dict) else results
    
    for item in items or []:
        score = item.get("score", 1.0)
        if score >= score_threshold:
            valid_results.append(item)
            
    return valid_results
