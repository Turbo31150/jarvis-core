#!/usr/bin/env python3
"""JARVIS A/B Tester — Compare LLM backends on same prompts for quality/speed"""

import redis
import requests
import time
import json
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:ab"

TEST_SUITES = {
    "speed": [
        "What is 5 * 7?",
        "Name the capital of France.",
        "Is Python a compiled language? Answer yes or no.",
    ],
    "quality": [
        "Explain in one sentence what a Redis sorted set is.",
        "Write a Python one-liner to reverse a list.",
        "What are the 3 main benefits of async/await in Python?",
    ],
    "reasoning": [
        "If A > B and B > C, what can we say about A and C?",
        "A task takes 3 workers 6 hours. How long for 9 workers?",
    ],
}

BACKENDS = {
    "m2_35b": ("http://192.168.1.26:1234/v1/chat/completions", "qwen/qwen3.5-35b-a3b", 30),
    "ol1_gemma3": ("http://127.0.0.1:11434/api/generate", "gemma3:4b", 20),
}


def query_backend(name: str, prompt: str) -> dict:
    url, model, timeout = BACKENDS[name]
    t0 = time.perf_counter()
    try:
        if "11434" in url:
            resp = requests.post(url, json={"model": model, "prompt": prompt, "stream": False, "options": {"num_predict": 150}}, timeout=timeout)
            text = resp.json().get("response", "")
        else:
            resp = requests.post(url, json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 150, "temperature": 0.1}, timeout=timeout)
            msg = resp.json()["choices"][0]["message"]
            text = msg.get("content") or msg.get("reasoning_content", "")
        lat = round((time.perf_counter() - t0) * 1000)
        return {"ok": True, "latency_ms": lat, "response": text, "length": len(text)}
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        return {"ok": False, "latency_ms": lat, "error": str(e)[:60]}


def run_ab(suite: str = "speed", backends: list = None) -> dict:
    test_prompts = TEST_SUITES.get(suite, TEST_SUITES["speed"])
    selected_backends = backends or list(BACKENDS.keys())
    results = {b: {"latencies": [], "ok": 0, "fail": 0} for b in selected_backends}

    for prompt in test_prompts:
        for bname in selected_backends:
            res = query_backend(bname, prompt)
            if res["ok"]:
                results[bname]["latencies"].append(res["latency_ms"])
                results[bname]["ok"] += 1
            else:
                results[bname]["fail"] += 1

    summary = {}
    winner = None
    best_lat = 999999
    for bname, data in results.items():
        lats = data["latencies"]
        avg_lat = sum(lats) // max(len(lats), 1)
        summary[bname] = {
            "ok": data["ok"],
            "fail": data["fail"],
            "avg_ms": avg_lat,
            "min_ms": min(lats) if lats else 0,
            "max_ms": max(lats) if lats else 0,
        }
        if avg_lat < best_lat and data["ok"] > 0:
            best_lat = avg_lat
            winner = bname

    ab_result = {
        "ts": datetime.now().isoformat()[:19],
        "suite": suite,
        "prompts": len(test_prompts),
        "winner": winner,
        "summary": summary,
    }
    r.setex(f"{PREFIX}:last:{suite}", 3600, json.dumps(ab_result))
    return ab_result


def leaderboard() -> dict:
    scores = {}
    for suite in TEST_SUITES:
        raw = r.get(f"{PREFIX}:last:{suite}")
        if raw:
            data = json.loads(raw)
            winner = data.get("winner")
            if winner:
                scores[winner] = scores.get(winner, 0) + 1
    return {"wins_by_backend": scores, "total_suites": len(TEST_SUITES)}


if __name__ == "__main__":
    import sys
    suite = sys.argv[1] if len(sys.argv) > 1 else "speed"
    print(f"A/B test: {suite}")
    res = run_ab(suite, ["ol1_gemma3"])  # Only test OL1 for speed
    print(f"Winner: {res['winner']}")
    for bname, s in res["summary"].items():
        print(f"  {bname}: {s['ok']}/{res['prompts']} ok, avg {s['avg_ms']}ms")
