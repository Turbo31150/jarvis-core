#!/usr/bin/env python3
"""JARVIS Daily Benchmark — Lance à 3h, stocke dans jarvis_master_index.db"""

import sqlite3
import time
import array
import requests
from datetime import datetime

DB = "/home/turbo/jarvis/core/jarvis_master_index.db"


def bench_cpu():
    t0 = time.perf_counter()
    n = 0
    for i in range(10**7):
        n += i
    return {
        "test": "cpu_loop_10M",
        "value": round((time.perf_counter() - t0) * 1000, 1),
        "unit": "ms",
        "ok": True,
    }


def bench_ram():
    SIZE = 128 * 1024 * 1024 // 8
    t0 = time.perf_counter()
    buf = array.array("d", bytes(SIZE * 8))
    bw = 128 / (time.perf_counter() - t0)
    return {
        "test": "ram_write_128mb",
        "value": round(bw, 0),
        "unit": "MB/s",
        "ok": True,
    }


def bench_llm(host, model, name):
    try:
        t0 = time.perf_counter()
        r = requests.post(
            f"{host}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "1+1="}],
                "max_tokens": 5,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=30,
        )
        d = r.json()
        msg = d["choices"][0]["message"]
        resp = msg.get("content") or msg.get("reasoning_content", "")
        return {
            "test": f"llm_{name}",
            "value": round((time.perf_counter() - t0) * 1000, 0),
            "unit": "ms",
            "ok": bool(resp),
        }
    except Exception:
        return {"test": f"llm_{name}", "value": -1, "unit": "ms", "ok": False}


def bench_ollama(model, name):
    try:
        t0 = time.perf_counter()
        r = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": model,
                "prompt": "1+1=",
                "stream": False,
                "options": {"num_predict": 3},
            },
            timeout=30,
        )
        resp = r.json().get("response", "")
        return {
            "test": f"llm_{name}",
            "value": round((time.perf_counter() - t0) * 1000, 0),
            "unit": "ms",
            "ok": bool(resp),
        }
    except:
        return {"test": f"llm_{name}", "value": -1, "unit": "ms", "ok": False}


def run_all():
    ts = datetime.now().isoformat()
    results = [
        bench_cpu(),
        bench_ram(),
        # qwen9b skipped: thinking bug blocks indefinitely
        bench_llm(
            "http://192.168.1.26:1234", "deepseek/deepseek-r1-0528-qwen3-8b", "m2_r1"
        ),
        bench_llm("http://192.168.1.26:1234", "qwen/qwen3.5-35b-a3b", "m2_qwen35b"),
        bench_ollama("gemma3:4b", "ol1_gemma3"),
        bench_ollama("qwen3:1.7b", "ol1_qwen17b"),
    ]
    db = sqlite3.connect(DB)
    for r in results:
        db.execute(
            "INSERT INTO benchmarks (metric, value, unit, backend, timestamp) VALUES (?,?,?,?,?)",
            (r["test"], r["value"], r["unit"], "local", ts),
        )
    db.commit()
    db.close()
    return results


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running daily benchmarks...")
    results = run_all()
    for r in results:
        print(
            f"  {'✅' if r.get('ok') else '❌'} {r['test']}: {r['value']} {r['unit']}"
        )
    print("Done — saved to jarvis_master_index.db")
