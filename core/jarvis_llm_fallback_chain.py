#!/usr/bin/env python3
"""JARVIS LLM Fallback Chain — Try M2→OL1→Gemini→OpenRouter in order"""
import subprocess, time, requests, redis
from datetime import datetime

r = redis.Redis(decode_responses=True)

CHAIN = [
    {"name": "m2_35b",   "type": "lmstudio", "host": "http://192.168.1.26:1234", "model": "qwen/qwen3.5-35b-a3b",    "timeout": 45},
    {"name": "m2_r1",    "type": "lmstudio", "host": "http://192.168.1.26:1234", "model": "deepseek/deepseek-r1-0528-qwen3-8b", "timeout": 60},
    {"name": "ol1_gemma","type": "ollama",   "host": "http://127.0.0.1:11434",   "model": "gemma3:4b",               "timeout": 30},
    {"name": "gemini",   "type": "cli",      "cmd":  "gemini --model gemini-2.5-flash -p",                          "timeout": 20},
]

def ask_with_fallback(prompt: str, max_tokens: int = 500) -> dict:
    for backend in CHAIN:
        try:
            from jarvis_circuit_breaker import is_allowed
            if not is_allowed(backend["name"]):
                continue
        except ImportError:
            pass

        t0 = time.perf_counter()
        try:
            if backend["type"] == "lmstudio":
                resp = requests.post(f"{backend['host']}/v1/chat/completions",
                    json={"model": backend["model"],
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens, "temperature": 0.1},
                    timeout=backend["timeout"])
                msg = resp.json()["choices"][0]["message"]
                result = msg.get("content") or msg.get("reasoning_content", "")
            elif backend["type"] == "ollama":
                resp = requests.post(f"{backend['host']}/api/generate",
                    json={"model": backend["model"], "prompt": prompt,
                          "stream": False, "options": {"num_predict": max_tokens}},
                    timeout=backend["timeout"])
                result = resp.json().get("response", "")
            elif backend["type"] == "cli":
                out = subprocess.run(f"{backend['cmd']} \"{prompt}\"", shell=True,
                    capture_output=True, text=True, timeout=backend["timeout"])
                result = out.stdout.strip()
            
            if result:
                lat = round((time.perf_counter() - t0) * 1000)
                r.setex(f"jarvis:fallback:last_backend", 300, backend["name"])
                return {"result": result, "backend": backend["name"], "latency_ms": lat}
        except Exception as e:
            try:
                from jarvis_circuit_breaker import record_failure
                record_failure(backend["name"])
            except ImportError:
                pass
            continue
    
    return {"result": "", "backend": "none", "error": "all backends failed"}

if __name__ == "__main__":
    print("Testing fallback chain...")
    res = ask_with_fallback("1+1=", max_tokens=10)
    print(f"Result: {res['result'][:30]} | Backend: {res['backend']} | {res.get('latency_ms','?')}ms")
