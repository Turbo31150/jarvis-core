#!/usr/bin/env python3
"""COMET Cluster — Distributed AI query engine for JARVIS."""
import json, time, re, os, requests, concurrent.futures
from pathlib import Path

CLUSTER = {
    "M1": {"url": "http://127.0.0.1:1234/v1/chat/completions", "model": "qwen/qwen3.5-9b", "role": "MASTER", "timeout": 15},
    "M2": {"url": "http://192.168.1.26:1234/v1/chat/completions", "model": "qwen/qwen3-8b", "role": "DETECTOR", "timeout": 15},
    "M3": {"url": "http://192.168.1.113:1234/v1/chat/completions", "model": "deepseek/deepseek-r1-0528-qwen3-8b", "role": "ORCHESTRATEUR", "timeout": 30},
}

# Fast models for quick queries
FAST_MODELS = {
    "M1": "qwen/qwen3.5-9b",
    "M2": "nvidia/nemotron-3-nano", 
    "M3": "nvidia/nemotron-3-nano",
}

# Deep models for complex reasoning
DEEP_MODELS = {
    "M1": "deepseek/deepseek-r1-0528-qwen3-8b",
    "M2": "qwen/qwen3-8b",
    "M3": "deepseek/deepseek-r1-0528-qwen3-8b",
}

def clean_response(text):
    """Strip <think> tags from deepseek-r1 responses."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

def query_node(name, prompt, model=None, temperature=0.3, max_tokens=500, timeout=None):
    """Query a single cluster node."""
    config = CLUSTER[name]
    model = model or config["model"]
    timeout = timeout or config["timeout"]
    
    try:
        start = time.time()
        resp = requests.post(config["url"], headers={"Content-Type": "application/json"}, json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens
        }, timeout=timeout)
        elapsed = time.time() - start
        content = clean_response(resp.json()["choices"][0]["message"]["content"])
        return {"node": name, "response": content, "time": round(elapsed, 1), "model": model, "ok": True}
    except Exception as e:
        return {"node": name, "response": str(e)[:100], "time": 0, "model": model, "ok": False}

def query_fast(prompt, node="M1"):
    """Fast query using lightweight model."""
    return query_node(node, prompt, model=FAST_MODELS.get(node), timeout=10)

def query_deep(prompt, node="M1"):
    """Deep reasoning query."""
    return query_node(node, prompt, model=DEEP_MODELS.get(node), timeout=60)

def query_parallel(prompts_dict, timeout=30):
    """Query multiple nodes in parallel. prompts_dict = {node_name: prompt}"""
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(prompts_dict)) as ex:
        futures = {ex.submit(query_node, name, prompt, timeout=timeout): name for name, prompt in prompts_dict.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            results[name] = future.result()
    return results

def consensus(prompt, nodes=None, min_agreement=2):
    """Query all nodes with same prompt, return consensus."""
    nodes = nodes or ["M1", "M2", "M3"]
    prompts = {n: prompt for n in nodes}
    results = query_parallel(prompts)
    
    ok_results = [r for r in results.values() if r["ok"]]
    return {
        "results": results,
        "total": len(ok_results),
        "consensus_reached": len(ok_results) >= min_agreement,
        "responses": [r["response"] for r in ok_results]
    }

def distribute(task_type, prompt):
    """Route task to appropriate node(s) based on type."""
    if task_type == "simple":
        return query_fast(prompt, "M1")
    elif task_type == "complex":
        return query_parallel({"M1": prompt, "M3": prompt}, timeout=60)
    elif task_type == "critical":
        return consensus(prompt)
    elif task_type == "code":
        return query_node("M2", prompt, model="deepseek/deepseek-coder-v2-lite-instruct")
    else:
        return query_fast(prompt)

def health_check():
    """Check cluster health."""
    results = {}
    for name, config in CLUSTER.items():
        try:
            resp = requests.get(config["url"].replace("/chat/completions", "/models"), timeout=3)
            models = [m["id"] for m in resp.json()["data"]]
            results[name] = {"status": "ONLINE", "models": len(models)}
        except:
            results[name] = {"status": "OFFLINE", "models": 0}
    return results

if __name__ == "__main__":
    print("COMET Cluster — Health Check")
    for name, status in health_check().items():
        print(f"  {name}: {status['status']} ({status['models']} models)")
    
    print("\nQuick test (M1 fast):")
    r = query_fast("Dis bonjour en 5 mots.")
    print(f"  {r['node']}: {r['response'][:100]} ({r['time']}s)")
