#!/usr/bin/env python3
"""JARVIS Adaptive Prompt — Auto-select persona + optimize prompt based on context"""

import redis

r = redis.Redis(decode_responses=True)
STATS_KEY = "jarvis:adaptive_prompt:stats"


def _detect_language(text: str) -> str:
    french_words = {
        "le",
        "la",
        "les",
        "de",
        "du",
        "des",
        "un",
        "une",
        "est",
        "et",
        "en",
        "je",
        "tu",
        "il",
        "nous",
        "vous",
        "ils",
        "que",
        "qui",
        "dans",
        "pour",
        "avec",
        "sur",
        "par",
        "si",
        "mais",
        "ou",
        "donc",
    }
    words = set(text.lower().split())
    overlap = len(words & french_words)
    return "fr" if overlap >= 2 else "en"


def _detect_persona(text: str, intent: str = None) -> str:
    """Pick best persona for the input."""
    text_lower = text.lower()
    if intent in ("code", "debug"):

        if any(w in text_lower for w in ["bug", "error", "traceback", "exception"]):
            return "debugger"
        return "code_expert"
    if intent in ("trading", "market"):
        return "trading_analyst"
    if _detect_language(text) == "fr":
        return "french"
    if len(text.split()) < 15:
        return "concise"
    return "jarvis"


def build(
    text: str, history: list = None, force_persona: str = None, optimize: bool = True
) -> dict:
    """
    Build an optimized prompt with the best persona for the input.
    Returns: {messages, persona_id, optimized_prompt, tokens_saved}
    """
    # 1. Classify intent
    intent = None
    try:
        from jarvis_intent_router import classify

        cls = classify(text)
        intent = cls.get("intent")
    except Exception:
        pass

    # 2. Select persona
    persona_id = force_persona or _detect_persona(text, intent)

    # 3. Optimize prompt
    optimized_text = text
    tokens_saved = 0
    if optimize:
        try:
            from jarvis_token_optimizer import optimize as do_opt

            result = do_opt(text)
            optimized_text = result["optimized"]
            tokens_saved = result["saved_tokens"]
        except Exception:
            pass

    # 4. Build messages
    try:
        from jarvis_persona_manager import build_messages

        messages = build_messages(optimized_text, history, persona_id)
    except Exception:
        messages = [{"role": "user", "content": optimized_text}]

    # 5. Compress history if needed
    total_tokens = sum(len(m.get("content", "").split()) * 4 // 3 for m in messages)
    if total_tokens > 2000:
        try:
            from jarvis_context_compressor import compress

            result = compress(messages, target_tokens=2000, strategy="key_facts")
            messages = result["messages"]
            tokens_saved += result.get("saved_tokens", 0)
        except Exception:
            pass

    r.hincrby(STATS_KEY, "builds", 1)
    r.hincrby(STATS_KEY, "tokens_saved", tokens_saved)

    return {
        "messages": messages,
        "persona_id": persona_id,
        "intent": intent,
        "language": _detect_language(text),
        "optimized_prompt": optimized_text,
        "tokens_saved": tokens_saved,
        "message_count": len(messages),
    }


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    tests = [
        "Could you please fix this Python bug where the function returns None instead of the expected value?",
        "Analyse le signal BTC sur 4h et donne-moi un signal long ou short.",
        "What is 2+2?",
        "Write a Redis health check that monitors memory usage and connection count.",
        "Traceback: AttributeError: 'NoneType' object has no attribute 'get' at line 42",
    ]
    for text in tests:
        result = build(text)
        print(
            f"  [{result['persona_id']:18s}] lang={result['language']} "
            f"intent={result.get('intent', '?'):15s} "
            f"msgs={result['message_count']} saved={result['tokens_saved']}t"
        )
    print(f"\nStats: {stats()}")
