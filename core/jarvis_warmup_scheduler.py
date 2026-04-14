#!/usr/bin/env python3
"""JARVIS Warmup Scheduler — Pre-warm LLM models before peak hours"""
import requests, redis, json, subprocess, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

# Peak hours: 8h-12h, 14h-18h, 20h-23h
PEAK_HOURS = list(range(8, 12)) + list(range(14, 18)) + list(range(20, 23))
WARMUP_PROMPT = "Hello"

MODELS_TO_WARM = [
    ("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b",          "m2_35b"),
    ("http://127.0.0.1:11434",   "gemma3:4b",                     "ol1_gemma"),
]

def is_peak_hour() -> bool:
    return datetime.now().hour in PEAK_HOURS

def warmup_model(host: str, model: str, name: str) -> bool:
    try:
        if "11434" in host:
            resp = requests.post(f"{host}/api/generate",
                json={"model": model, "prompt": WARMUP_PROMPT, "stream": False,
                      "options": {"num_predict": 1}}, timeout=20)
            ok = bool(resp.json().get("response", ""))
        else:
            resp = requests.post(f"{host}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": WARMUP_PROMPT}],
                      "max_tokens": 1, "temperature": 0}, timeout=20)
            ok = bool(resp.json()["choices"][0]["message"].get("content", ""))
        r.setex(f"jarvis:warmup:{name}", 600, "warm" if ok else "cold")
        return ok
    except:
        r.setex(f"jarvis:warmup:{name}", 600, "cold")
        return False

def run_warmup():
    if not is_peak_hour():
        return {"skipped": "not peak hour"}
    results = {}
    for host, model, name in MODELS_TO_WARM:
        ok = warmup_model(host, model, name)
        results[name] = "warm" if ok else "cold"
    r.setex("jarvis:warmup:last", 3600, json.dumps({"ts": datetime.now().isoformat()[:19], **results}))
    return results

if __name__ == "__main__":
    h = datetime.now().hour
    print(f"Hour: {h} | Peak: {is_peak_hour()}")
    res = run_warmup()
    print(f"Warmup: {res}")
