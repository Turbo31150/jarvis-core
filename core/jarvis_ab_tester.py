#!/usr/bin/env python3
"""JARVIS A/B Tester — Compare two LLM models on same prompts for quality evaluation"""
import requests, redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

def query_model(host: str, model: str, prompt: str, timeout: int = 30) -> dict:
    t0 = time.perf_counter()
    try:
        if "11434" in host:
            resp = requests.post(f"{host}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "options": {"num_predict": 200}}, timeout=timeout)
            result = resp.json().get("response", "")
        else:
            resp = requests.post(f"{host}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 200, "temperature": 0.1}, timeout=timeout)
            msg = resp.json()["choices"][0]["message"]
            result = msg.get("content") or msg.get("reasoning_content", "")
        lat = round((time.perf_counter() - t0) * 1000)
        return {"result": result, "latency_ms": lat, "ok": bool(result)}
    except Exception as e:
        return {"result": "", "latency_ms": -1, "ok": False, "error": str(e)[:80]}

def run_ab_test(prompt: str, 
                model_a: tuple = ("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b"),
                model_b: tuple = ("http://127.0.0.1:11434", "gemma3:4b")) -> dict:
    
    res_a = query_model(*model_a, prompt)
    res_b = query_model(*model_b, prompt)
    
    result = {
        "prompt": prompt[:100],
        "ts": datetime.now().isoformat()[:19],
        "model_a": {"model": model_a[1], **res_a},
        "model_b": {"model": model_b[1], **res_b},
        "winner_latency": "a" if res_a["latency_ms"] < res_b["latency_ms"] else "b",
        "winner_length":  "a" if len(res_a["result"]) > len(res_b["result"]) else "b",
    }
    r.lpush("jarvis:ab_results", json.dumps(result))
    r.ltrim("jarvis:ab_results", 0, 49)
    return result

if __name__ == "__main__":
    print("Running A/B test...")
    res = run_ab_test("Explain in 2 sentences what is a neural network.")
    print(f"Model A ({res['model_a']['model'][:20]}): {res['model_a']['latency_ms']}ms, {len(res['model_a']['result'])} chars")
    print(f"Model B ({res['model_b']['model'][:20]}): {res['model_b']['latency_ms']}ms, {len(res['model_b']['result'])} chars")
    print(f"Winner latency: {res['winner_latency']} | Winner length: {res['winner_length']}")
