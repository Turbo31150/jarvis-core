#!/usr/bin/env python3
"""JARVIS Persona Manager — Manage system prompts and personas for different LLM use cases"""

import redis
import json
import time

r = redis.Redis(decode_responses=True)

PERSONAS_KEY = "jarvis:personas"
ACTIVE_KEY = "jarvis:personas:active"
STATS_KEY = "jarvis:personas:stats"

BUILTIN_PERSONAS = {
    "jarvis": {
        "name": "JARVIS",
        "system_prompt": "You are JARVIS, an advanced AI cluster management system. Be concise, technical, and direct. No preamble. Answer in the same language as the question.",
        "temperature": 0.1,
        "max_tokens": 500,
        "tags": ["default", "system"],
    },
    "code_expert": {
        "name": "Code Expert",
        "system_prompt": "You are an expert software engineer. Provide clean, efficient, production-ready code. Include only necessary comments. Use best practices. No explanations unless asked.",
        "temperature": 0.05,
        "max_tokens": 1000,
        "tags": ["code", "dev"],
    },
    "trading_analyst": {
        "name": "Trading Analyst",
        "system_prompt": "You are a quantitative trading analyst. Analyze market data objectively. Provide confidence scores (0-1). Always include risk assessment. Be data-driven.",
        "temperature": 0.1,
        "max_tokens": 600,
        "tags": ["trading", "finance"],
    },
    "concise": {
        "name": "Concise Responder",
        "system_prompt": "Answer in 1-3 sentences maximum. No preamble. No explanation unless asked. Facts only.",
        "temperature": 0.1,
        "max_tokens": 100,
        "tags": ["fast", "brief"],
    },
    "french": {
        "name": "Assistant Français",
        "system_prompt": "Tu es un assistant technique expert. Réponds toujours en français. Sois précis, concis, et direct. Pas de préambule.",
        "temperature": 0.1,
        "max_tokens": 500,
        "tags": ["french", "language"],
    },
    "debugger": {
        "name": "Debug Expert",
        "system_prompt": "You are a debugging specialist. Identify root causes, not symptoms. Provide minimal reproducible examples. Suggest systematic fixes. Be methodical.",
        "temperature": 0.05,
        "max_tokens": 800,
        "tags": ["code", "debug"],
    },
}


def bootstrap() -> int:
    for pid, persona in BUILTIN_PERSONAS.items():
        r.hset(
            PERSONAS_KEY,
            pid,
            json.dumps(
                {**persona, "id": pid, "builtin": True, "created_at": time.time()}
            ),
        )
    # Set default active
    r.set(ACTIVE_KEY, "jarvis")
    return len(BUILTIN_PERSONAS)


def register(
    persona_id: str,
    name: str,
    system_prompt: str,
    temperature: float = 0.1,
    max_tokens: int = 500,
    tags: list = None,
) -> dict:
    persona = {
        "id": persona_id,
        "name": name,
        "system_prompt": system_prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "tags": tags or [],
        "builtin": False,
        "created_at": time.time(),
    }
    r.hset(PERSONAS_KEY, persona_id, json.dumps(persona))
    return persona


def get(persona_id: str) -> dict | None:
    raw = r.hget(PERSONAS_KEY, persona_id)
    return json.loads(raw) if raw else None


def get_active() -> dict:
    active_id = r.get(ACTIVE_KEY) or "jarvis"
    return get(active_id) or BUILTIN_PERSONAS["jarvis"]


def set_active(persona_id: str) -> bool:
    if not r.hexists(PERSONAS_KEY, persona_id):
        return False
    r.set(ACTIVE_KEY, persona_id)
    r.hincrby(STATS_KEY, f"activated:{persona_id}", 1)
    return True


def build_messages(
    user_prompt: str, history: list = None, persona_id: str = None
) -> list:
    """Build a messages array with persona system prompt + history + user prompt."""
    persona = get(persona_id) if persona_id else get_active()
    messages = [{"role": "system", "content": persona["system_prompt"]}]
    if history:
        messages.extend(history[-10:])  # last 10 turns
    messages.append({"role": "user", "content": user_prompt})
    return messages


def list_personas(tag: str = None) -> list:
    all_p = [json.loads(v) for v in r.hvals(PERSONAS_KEY)]
    if tag:
        all_p = [p for p in all_p if tag in p.get("tags", [])]
    return sorted(all_p, key=lambda x: x["name"])


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    active = r.get(ACTIVE_KEY) or "jarvis"
    return {
        "total_personas": r.hlen(PERSONAS_KEY),
        "active": active,
        "activations": {k.replace("activated:", ""): int(v) for k, v in s.items()},
    }


if __name__ == "__main__":
    n = bootstrap()
    print(f"Bootstrapped {n} personas")

    for pid in ["jarvis", "code_expert", "trading_analyst", "concise"]:
        p = get(pid)
        prompt_preview = p["system_prompt"][:60]
        print(f"  [{pid:18s}] t={p['temperature']} | {prompt_preview}…")

    # Build messages for a code task
    set_active("code_expert")
    msgs = build_messages("Write a Redis health check function")
    print(f"\nActive: {get_active()['name']}")
    print(f"Messages built: {len(msgs)} (system + user)")
    print(f"Stats: {stats()}")
