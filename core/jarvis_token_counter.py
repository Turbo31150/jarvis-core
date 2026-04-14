#!/usr/bin/env python3
"""JARVIS Token Counter — Estimate and track token usage across LLM backends"""

import redis
import re
import json
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:tokens"


def estimate_tokens(text: str) -> int:
    """Fast token estimation: ~4 chars per token (Claude/GPT heuristic)"""
    if not text:
        return 0
    # More accurate: count words + punctuation separately
    words = len(re.findall(r'\w+', text))
    punct = len(re.findall(r'[^\w\s]', text))
    # Approximate: words * 1.3 + punctuation * 0.5
    return max(1, int(words * 1.3 + punct * 0.5))


def record(model: str, input_text: str, output_text: str, task_type: str = "default"):
    input_tokens = estimate_tokens(input_text)
    output_tokens = estimate_tokens(output_text)
    total = input_tokens + output_tokens

    today = datetime.now().strftime("%Y-%m-%d")
    hour = datetime.now().strftime("%Y-%m-%d:%H")

    pipe = r.pipeline()
    # Daily
    pipe.hincrby(f"{PREFIX}:daily:{today}:{model}", "input", input_tokens)
    pipe.hincrby(f"{PREFIX}:daily:{today}:{model}", "output", output_tokens)
    pipe.hincrby(f"{PREFIX}:daily:{today}:{model}", "calls", 1)
    pipe.expire(f"{PREFIX}:daily:{today}:{model}", 86400 * 30)
    # Hourly
    pipe.incrby(f"{PREFIX}:hourly:{hour}:{model}", total)
    pipe.expire(f"{PREFIX}:hourly:{hour}:{model}", 86400 * 3)
    # Task type
    pipe.hincrby(f"{PREFIX}:task:{today}:{task_type}", "tokens", total)
    pipe.expire(f"{PREFIX}:task:{today}:{task_type}", 86400 * 7)
    pipe.execute()

    return {"input": input_tokens, "output": output_tokens, "total": total}


def daily_usage(date: str = None) -> dict:
    day = date or datetime.now().strftime("%Y-%m-%d")
    result = {}
    for key in r.scan_iter(f"{PREFIX}:daily:{day}:*"):
        model = key.split(":")[-1]
        data = r.hgetall(key)
        result[model] = {
            "input": int(data.get("input", 0)),
            "output": int(data.get("output", 0)),
            "total": int(data.get("input", 0)) + int(data.get("output", 0)),
            "calls": int(data.get("calls", 0)),
        }
    return {"date": day, "by_model": result, "total_tokens": sum(v["total"] for v in result.values())}


def hourly_breakdown(model: str = "all") -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    hours = {}
    pattern = f"{PREFIX}:hourly:{today}:*" if model == "all" else f"{PREFIX}:hourly:{today}:*{model}*"
    for key in r.scan_iter(pattern):
        hour = key.split(":")[3]
        count = int(r.get(key) or 0)
        hours[hour] = hours.get(hour, 0) + count
    return hours


def stats() -> dict:
    usage = daily_usage()
    return {
        "today": usage,
        "hourly": hourly_breakdown(),
    }


if __name__ == "__main__":
    # Test
    samples = [
        ("gemma3:4b", "What is 2+2?", "The answer is 4.", "fast"),
        ("qwen3.5-35b", "Write a Python function to sort a list in reverse order.", "def reverse_sort(lst): return sorted(lst, reverse=True)", "code"),
        ("gemma3:4b", "Summarize in one line: Redis is a fast in-memory data store.", "Redis is a fast in-memory data store.", "summary"),
    ]
    for model, inp, out, task in samples:
        counts = record(model, inp, out, task)
        print(f"  {model}: {counts['input']}+{counts['output']}={counts['total']} tokens")

    usage = daily_usage()
    print(f"\nToday: {usage['total_tokens']} total tokens")
    for m, d in usage["by_model"].items():
        print(f"  {m}: {d['total']} tokens ({d['calls']} calls)")
