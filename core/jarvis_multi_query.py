#!/usr/bin/env python3
"""JARVIS Multi Query — Fan-out same prompt to multiple backends and aggregate results"""

import redis
import time
import threading

r = redis.Redis(decode_responses=True)
STATS_KEY = "jarvis:multi_query:stats"


def _call_backend(backend_cfg: dict, prompt: str, results: dict, key: str):
    """Call a single backend and store result."""
    host = backend_cfg["url"]
    model = backend_cfg["model"]
    timeout = backend_cfg.get("timeout", 20)
    t0 = time.perf_counter()
    try:
        import requests

        if "11434" in host:
            resp = requests.post(
                f"{host}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": 400},
                },
                timeout=timeout,
            )
            text = resp.json().get("response", "")
        else:
            resp = requests.post(
                f"{host}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.1,
                },
                timeout=timeout,
            )
            choices = resp.json().get("choices", [])
            text = choices[0]["message"].get("content", "") if choices else ""
        lat = round((time.perf_counter() - t0) * 1000)
        results[key] = {
            "text": text,
            "ok": bool(text),
            "latency_ms": lat,
            "backend": key,
        }
    except Exception as e:
        lat = round((time.perf_counter() - t0) * 1000)
        results[key] = {
            "text": "",
            "ok": False,
            "error": str(e)[:60],
            "latency_ms": lat,
            "backend": key,
        }


BACKENDS = {
    "m32_mistral": {
        "url": "http://192.168.1.113:1234",
        "model": "mistral-7b-instruct-v0.3",
        "timeout": 25,
    },
    "ol1_gemma3": {
        "url": "http://127.0.0.1:11434",
        "model": "gemma3:4b",
        "timeout": 20,
    },
}


def fan_out(prompt: str, backends: list = None, timeout: float = 30) -> dict:
    """Send same prompt to multiple backends in parallel, return all results."""
    target_backends = backends or list(BACKENDS.keys())
    results = {}
    threads = []
    for key in target_backends:
        cfg = BACKENDS.get(key)
        if not cfg:
            continue
        t = threading.Thread(target=_call_backend, args=(cfg, prompt, results, key))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    ok = {k: v for k, v in results.items() if v["ok"]}
    r.hincrby(STATS_KEY, "fan_out_calls", 1)
    r.hincrby(STATS_KEY, "backends_ok", len(ok))
    return {"results": results, "ok_count": len(ok), "total": len(target_backends)}


def consensus(prompt: str, backends: list = None) -> dict:
    """Fan-out and return the best response by reranking."""
    fan = fan_out(prompt, backends)
    items = [v for v in fan["results"].values() if v["ok"]]
    if not items:
        return {"answer": "", "ok": False, "results": fan["results"]}
    try:
        from jarvis_reranker import pick_best

        best = pick_best(items, query=prompt)
        return {
            "answer": best["text"],
            "backend": best["backend"],
            "score": best.get("_rerank_score", 0),
            "ok": True,
            "all_results": fan["results"],
        }
    except Exception:
        # Fallback: pick fastest
        fastest = min(items, key=lambda x: x["latency_ms"])
        return {"answer": fastest["text"], "backend": fastest["backend"], "ok": True}


def race(prompt: str, backends: list = None) -> dict:
    """Return the first successful response (fastest wins)."""
    target_backends = backends or list(BACKENDS.keys())
    winner = [None]
    done = threading.Event()

    def run(key):
        cfg = BACKENDS.get(key)
        if not cfg:
            return
        result = {}
        _call_backend(cfg, prompt, result, key)
        res = result.get(key, {})
        if res.get("ok") and not done.is_set():
            done.set()
            winner[0] = res

    threads = [threading.Thread(target=run, args=(k,)) for k in target_backends]
    for t in threads:
        t.start()
    done.wait(timeout=25)
    r.hincrby(STATS_KEY, "race_calls", 1)
    return winner[0] or {"ok": False, "error": "all backends failed"}


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {k: int(v) for k, v in s.items()}


if __name__ == "__main__":
    prompt = "What is 1+1? Reply with just the number."
    print(f"Fan-out: '{prompt}'")
    result = fan_out(prompt)
    for backend, res in result["results"].items():
        icon = "✅" if res["ok"] else "❌"
        print(
            f"  {icon} {backend} ({res['latency_ms']}ms): {res.get('text', res.get('error', ''))[:50]}"
        )

    print("\nRace winner:")
    winner = race(prompt)
    print(
        f"  {winner.get('backend', '?')} ({winner.get('latency_ms', '?')}ms): {winner.get('text', '')[:50]}"
    )
    print(f"\nStats: {stats()}")
