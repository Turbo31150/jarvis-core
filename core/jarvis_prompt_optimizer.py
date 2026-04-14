#!/usr/bin/env python3
"""JARVIS Prompt Optimizer — Optimize prompts for better LLM performance"""

import redis
import json
import re
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:prompt_opt"

OPTIMIZATION_RULES = [
    {"name": "add_conciseness",   "trigger": lambda p: len(p) > 500,          "transform": lambda p: f"Be concise. {p}"},
    {"name": "add_format",        "trigger": lambda p: "list" in p.lower() and "\n" not in p, "transform": lambda p: f"{p}\nRespond as a numbered list."},
    {"name": "add_no_preamble",   "trigger": lambda p: len(p) < 200,          "transform": lambda p: f"{p}\nAnswer directly without preamble."},
    {"name": "add_json_format",   "trigger": lambda p: "json" in p.lower(),   "transform": lambda p: f"{p}\nRespond with valid JSON only."},
    {"name": "strip_filler",      "trigger": lambda p: "please" in p.lower() or "could you" in p.lower(), "transform": lambda p: re.sub(r'(?i)(please |could you |can you )', '', p).strip()},
]

TASK_PROMPTS = {
    "code":      "You are an expert Python developer. Write clean, minimal code. ",
    "reasoning": "Think step by step. Show your reasoning. ",
    "summary":   "Summarize concisely in plain language. ",
    "classify":  "Classify the following. Answer with just the category name. ",
    "trading":   "You are an expert crypto trader. Be precise and data-driven. ",
}


def optimize(prompt: str, task_type: str = "default", aggressive: bool = False) -> dict:
    original = prompt
    applied_rules = []

    # Add task-specific prefix
    if task_type in TASK_PROMPTS:
        prefix = TASK_PROMPTS[task_type]
        if not prompt.startswith(prefix[:20]):
            prompt = prefix + prompt
            applied_rules.append("task_prefix")

    # Apply rules
    for rule in OPTIMIZATION_RULES:
        try:
            if rule["trigger"](prompt):
                new_prompt = rule["transform"](prompt)
                if new_prompt != prompt:
                    prompt = new_prompt
                    applied_rules.append(rule["name"])
        except Exception:
            pass

    # Aggressive: compress whitespace
    if aggressive:
        prompt = " ".join(prompt.split())
        applied_rules.append("whitespace_compress")

    result = {
        "original": original[:200],
        "optimized": prompt,
        "original_len": len(original),
        "optimized_len": len(prompt),
        "rules_applied": applied_rules,
        "delta_chars": len(prompt) - len(original),
    }
    r.hincrby(f"{PREFIX}:stats", "optimized", 1)
    return result


def batch_optimize(prompts: list, task_type: str = "default") -> list:
    return [optimize(p, task_type) for p in prompts]


def stats() -> dict:
    data = r.hgetall(f"{PREFIX}:stats")
    return {"total_optimized": int(data.get("optimized", 0))}


if __name__ == "__main__":
    tests = [
        ("What is redis?", "default"),
        ("Please could you write a python function to sort a list", "code"),
        ("Give me a list of 5 best practices for REST APIs", "default"),
        ('Return user data as json format: {"name": "test"}', "default"),
    ]
    for prompt, task in tests:
        res = optimize(prompt, task)
        print(f"  [{task}] '{prompt[:40]}...' → rules={res['rules_applied']}")
    print(f"Stats: {stats()}")
