#!/usr/bin/env python3
"""JARVIS Intent Router — Classify user intent and route to specialized handlers"""

import redis
import json
import time
import re

r = redis.Redis(decode_responses=True)

STATS_KEY = "jarvis:intent_router:stats"
HISTORY_KEY = "jarvis:intent_router:history"

INTENT_PATTERNS = {
    "gpu_status": [r"gpu", r"vram", r"temp[eé]", r"thermal", r"°c"],
    "llm_query": [
        r"demande[- ]?toi",
        r"ask\s+llm",
        r"génère",
        r"écris",
        r"explain",
        r"what is",
    ],
    "cluster_health": [r"cluster", r"nœud", r"node", r"health", r"status", r"santé"],
    "trading": [r"btc", r"eth", r"trade", r"signal", r"market", r"crypto", r"buy|sell"],
    "code": [r"def\s+\w", r"class\s+\w", r"bug", r"fix", r"refactor", r"code"],
    "system_ops": [r"restart", r"stop", r"start", r"service", r"systemctl", r"deploy"],
    "search": [r"cherche", r"search", r"find", r"trouve", r"où est"],
    "memory": [r"souviens", r"remember", r"mémo", r"note", r"save this"],
    "schedule": [r"planifie", r"schedule", r"every\s+\d", r"cron", r"à \d+h"],
    "alert": [r"alerte", r"alert", r"notif", r"warn", r"urgence"],
    "summarize": [r"résume", r"summar", r"tldr", r"en bref"],
}

HANDLERS = {
    "gpu_status": {
        "module": "jarvis_gpu_balancer",
        "func": "get_gpu_load",
        "backend": "ol1",
    },
    "cluster_health": {
        "module": "jarvis_health_aggregator",
        "func": "aggregate",
        "backend": "ol1",
    },
    "trading": {
        "module": "jarvis_llm_router",
        "func": "ask",
        "backend": "m2",
        "task_type": "trading",
    },
    "code": {
        "module": "jarvis_llm_router",
        "func": "ask",
        "backend": "m2",
        "task_type": "code",
    },
    "llm_query": {
        "module": "jarvis_llm_router",
        "func": "ask",
        "backend": "m2",
        "task_type": "default",
    },
    "summarize": {
        "module": "jarvis_llm_router",
        "func": "ask",
        "backend": "ol1",
        "task_type": "summary",
    },
    "system_ops": {
        "module": "jarvis_runbook",
        "func": "list_runbooks",
        "backend": "local",
    },
    "search": {"module": "jarvis_knowledge_base", "func": "search", "backend": "local"},
    "memory": {"module": "jarvis_memory_store", "func": "store", "backend": "local"},
    "alert": {"module": "jarvis_alert_router", "func": "send", "backend": "local"},
    "schedule": {"module": "jarvis_job_scheduler", "func": "stats", "backend": "local"},
}


def classify(text: str) -> dict:
    """Classify intent with confidence scores."""
    text_lower = text.lower()
    scores = {}
    for intent, patterns in INTENT_PATTERNS.items():
        matches = sum(1 for p in patterns if re.search(p, text_lower))
        if matches > 0:
            scores[intent] = round(matches / len(patterns), 3)

    if not scores:
        top_intent, confidence = "llm_query", 0.5
    else:
        top_intent = max(scores, key=scores.get)
        confidence = scores[top_intent]

    handler = HANDLERS.get(top_intent, HANDLERS["llm_query"])
    result = {
        "intent": top_intent,
        "confidence": confidence,
        "all_scores": dict(
            sorted(scores.items(), key=lambda x: x[1], reverse=True)[:3]
        ),
        "handler": handler,
        "backend": handler.get("backend", "m2"),
    }
    # Log
    r.lpush(HISTORY_KEY, json.dumps({**result, "text": text[:100], "ts": time.time()}))
    r.ltrim(HISTORY_KEY, 0, 199)
    r.hincrby(STATS_KEY, f"intent:{top_intent}", 1)
    return result


def route_and_handle(text: str) -> dict:
    """Classify intent and execute the appropriate handler."""
    classification = classify(text)
    intent = classification["intent"]
    handler = classification["handler"]

    try:
        import importlib

        mod = importlib.import_module(handler["module"])
        fn = getattr(mod, handler["func"])

        if handler["func"] == "ask":
            task_type = handler.get("task_type", "default")
            result = fn(text, task_type)
        elif handler["func"] == "search":
            result = fn(text, limit=3)
        else:
            result = fn()

        return {**classification, "result": result, "handled": True}
    except Exception as e:
        return {**classification, "handled": False, "error": str(e)[:80]}


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    intents = {
        k.replace("intent:", ""): int(v)
        for k, v in s.items()
        if k.startswith("intent:")
    }
    return {"total_classified": sum(intents.values()), "by_intent": intents}


if __name__ == "__main__":
    queries = [
        "What is the GPU temperature on GPU0?",
        "Résume ce texte en 2 phrases",
        "BTC signal long ou short maintenant?",
        "Fix this Python bug: def foo(x): return x/0",
        "Cluster health check please",
        "Search for Redis configuration",
        "What is 2+2?",
    ]
    for q in queries:
        r_ = classify(q)
        print(f"  [{r_['intent']:15s} {r_['confidence']:.2f}] {q[:50]}")
    print(f"\nStats: {stats()}")
