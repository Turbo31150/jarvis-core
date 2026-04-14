#!/usr/bin/env python3
"""JARVIS Token Optimizer — Minimize token usage while preserving prompt quality"""

import redis
import re

r = redis.Redis(decode_responses=True)
STATS_KEY = "jarvis:token_opt:stats"

# Filler phrases to strip
FILLERS = [
    r"(?i)please\s+",
    r"(?i)could you\s+",
    r"(?i)can you\s+",
    r"(?i)i would like you to\s+",
    r"(?i)i need you to\s+",
    r"(?i)as an ai[^,\.]*[,\.]?\s*",
    r"(?i)as a language model[^,\.]*[,\.]?\s*",
    r"(?i)note that\s+",
    r"(?i)keep in mind that\s+",
    r"(?i)it is important to note\s+",
    r"(?i)make sure to\s+",
    r"(?i)be sure to\s+",
]

# Abbreviation map for common verbose phrases
ABBREV = {
    "in order to": "to",
    "due to the fact that": "because",
    "at this point in time": "now",
    "in the event that": "if",
    "with regard to": "regarding",
    "it is worth noting that": "",
    "it should be noted that": "",
    "for the purpose of": "for",
    "in addition to": "also",
    "a large number of": "many",
    "the majority of": "most",
    "in the near future": "soon",
}


def _count_tokens(text: str) -> int:
    """Rough token count estimate."""
    return len(text.split()) * 4 // 3


def strip_fillers(text: str) -> str:
    for pattern in FILLERS:
        text = re.sub(pattern, "", text)
    return text.strip()


def replace_verbose(text: str) -> str:
    for verbose, concise in ABBREV.items():
        text = re.sub(re.escape(verbose), concise, text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", text).strip()


def compress_whitespace(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def truncate_to_budget(text: str, max_tokens: int = 800) -> str:
    words = text.split()
    budget_words = int(max_tokens * 3 / 4)
    if len(words) <= budget_words:
        return text
    return " ".join(words[:budget_words]) + "…"


def optimize(prompt: str, max_tokens: int = 800, aggressive: bool = False) -> dict:
    """Optimize a prompt to reduce token count."""
    original_tokens = _count_tokens(prompt)
    text = prompt

    text = strip_fillers(text)
    text = replace_verbose(text)
    text = compress_whitespace(text)

    if aggressive:
        # Remove redundant adjectives and adverbs (simple heuristic)
        text = re.sub(
            r"\b(very|really|quite|rather|extremely|absolutely|basically|literally)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = truncate_to_budget(text, max_tokens)

    optimized_tokens = _count_tokens(text)
    saved = original_tokens - optimized_tokens
    pct = round(saved / max(original_tokens, 1) * 100, 1)

    r.hincrby(STATS_KEY, "prompts_optimized", 1)
    r.hincrby(STATS_KEY, "tokens_saved", max(0, saved))

    return {
        "original": prompt,
        "optimized": text,
        "original_tokens": original_tokens,
        "optimized_tokens": optimized_tokens,
        "saved_tokens": saved,
        "reduction_pct": pct,
    }


def batch_optimize(prompts: list, max_tokens: int = 800) -> list:
    return [optimize(p, max_tokens) for p in prompts]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    tests = [
        "Could you please explain in detail what the current GPU temperature is and make sure to include all relevant information?",
        "As an AI language model, I need you to please summarize the following text in order to make it easier to understand.",
        "Please note that it is important to keep in mind that due to the fact that the cluster is busy, in the near future we may need to scale up.",
        "Fix this bug: def divide(x, y): return x/y",
    ]
    total_saved = 0
    for prompt in tests:
        result = optimize(prompt, aggressive=True)
        total_saved += result["saved_tokens"]
        print(
            f"  -{result['reduction_pct']:4.1f}% | {result['original_tokens']:3d}→{result['optimized_tokens']:3d}t | {result['optimized'][:60]}"
        )
    print(f"\nTotal saved: {total_saved} tokens | Stats: {stats()}")
