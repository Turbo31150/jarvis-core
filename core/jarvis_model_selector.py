#!/usr/bin/env python3
"""JARVIS Model Selector — Smart model selection based on task + availability + latency"""

import redis
import json
import requests
import time
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:model_sel"

# All available models across all nodes
MODEL_CATALOG = {
    # M2
    "m2_qwen35b":   {"node": "M2",  "url": "http://192.168.1.26:1234",  "model": "qwen/qwen3.5-35b-a3b",               "strengths": ["code","analysis","default"], "speed": "medium"},
    "m2_r1":        {"node": "M2",  "url": "http://192.168.1.26:1234",  "model": "deepseek/deepseek-r1-0528-qwen3-8b", "strengths": ["reasoning","trading","math"],  "speed": "slow"},
    # M32
    "m32_mistral":  {"node": "M32", "url": "http://192.168.1.113:1234", "model": "mistral-7b-instruct-v0.3",           "strengths": ["instruct","chat","default"],   "speed": "fast"},
    "m32_r1":       {"node": "M32", "url": "http://192.168.1.113:1234", "model": "deepseek/deepseek-r1-0528-qwen3-8b", "strengths": ["reasoning","analysis"],         "speed": "slow"},
    "m32_phi":      {"node": "M32", "url": "http://192.168.1.113:1234", "model": "phi-3.1-mini-128k-instruct",         "strengths": ["fast","classify","short"],      "speed": "fast"},
    # OL1
    "ol1_gemma3":   {"node": "OL1", "url": "http://127.0.0.1:11434",   "model": "gemma3:4b",                          "strengths": ["fast","classify","summary"],    "speed": "fast", "api": "ollama"},
    "ol1_deepseek": {"node": "OL1", "url": "http://127.0.0.1:11434",   "model": "deepseek-r1:7b",                     "strengths": ["reasoning"],                   "speed": "medium", "api": "ollama"},
}

TASK_PREFERENCES = {
    "fast":      ["ol1_gemma3", "m32_phi", "m32_mistral"],
    "classify":  ["ol1_gemma3", "m32_phi"],
    "summary":   ["ol1_gemma3", "m32_mistral"],
    "code":      ["m2_qwen35b", "m32_mistral"],
    "reasoning": ["m2_r1", "m32_r1", "ol1_deepseek"],
    "instruct":  ["m32_mistral", "m2_qwen35b"],
    "trading":   ["m2_r1", "m32_r1"],
    "default":   ["m32_mistral", "m2_qwen35b", "ol1_gemma3"],
}


def probe_model(model_key: str) -> dict:
    cfg = MODEL_CATALOG[model_key]
    t0 = time.perf_counter()
    try:
        if cfg.get("api") == "ollama":
            resp = requests.post(f"{cfg['url']}/api/generate", json={"model": cfg["model"], "prompt": "hi", "stream": False, "options": {"num_predict": 3}}, timeout=5)
            ok = resp.status_code == 200
        else:
            resp = requests.get(f"{cfg['url']}/v1/models", timeout=3)
            ok = resp.status_code == 200
        lat = round((time.perf_counter() - t0) * 1000)
        r.hset(f"{PREFIX}:probes:{model_key}", mapping={"ok": str(ok), "latency_ms": lat, "ts": time.strftime("%H:%M:%S")})
        r.expire(f"{PREFIX}:probes:{model_key}", 300)
        return {"model_key": model_key, "ok": ok, "latency_ms": lat}
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        r.hset(f"{PREFIX}:probes:{model_key}", mapping={"ok": "False", "latency_ms": lat, "ts": time.strftime("%H:%M:%S"), "error": str(e)[:40]})
        r.expire(f"{PREFIX}:probes:{model_key}", 300)
        return {"model_key": model_key, "ok": False, "latency_ms": lat}


def select(task_type: str = "default") -> dict:
    preferences = TASK_PREFERENCES.get(task_type, TASK_PREFERENCES["default"])
    for model_key in preferences:
        probe_data = r.hgetall(f"{PREFIX}:probes:{model_key}")
        if probe_data and probe_data.get("ok") == "True":
            cfg = MODEL_CATALOG[model_key]
            return {"model_key": model_key, "node": cfg["node"], "url": cfg["url"], "model": cfg["model"], "api": cfg.get("api", "openai"), "latency_ms": int(probe_data["latency_ms"])}
    # No cached probe — probe first available
    for model_key in preferences:
        probe_result = probe_model(model_key)
        if probe_result["ok"]:
            cfg = MODEL_CATALOG[model_key]
            return {"model_key": model_key, "node": cfg["node"], "url": cfg["url"], "model": cfg["model"], "api": cfg.get("api", "openai"), "latency_ms": probe_result["latency_ms"]}
    return {"error": f"no model available for {task_type}"}


def probe_all() -> dict:
    import threading
    results = {}
    threads = []
    def _probe(k):
        results[k] = probe_model(k)
    for k in MODEL_CATALOG:
        t = threading.Thread(target=_probe, args=(k,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=10)
    available = sum(1 for v in results.values() if v.get("ok"))
    return {"total": len(MODEL_CATALOG), "available": available, "results": results}


if __name__ == "__main__":
    import sys
    if "--probe" in sys.argv:
        print("Probing all models...")
        res = probe_all()
        print(f"Available: {res['available']}/{res['total']}")
        for k, v in res["results"].items():
            icon = "✅" if v.get("ok") else "❌"
            print(f"  {icon} {k}: {v.get('latency_ms')}ms")
    else:
        for task in ["fast", "code", "reasoning", "instruct"]:
            sel = select(task)
            if "error" not in sel:
                print(f"  {task}: {sel['model_key']} ({sel['node']}) {sel.get('latency_ms','?')}ms")
            else:
                print(f"  {task}: {sel['error']}")
