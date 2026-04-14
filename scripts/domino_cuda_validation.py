#!/usr/bin/env python3
"""
Domino Pipeline: Validation CUDA LM Studio — charge évolutive auto-alimentée
Étapes: probe → warmup → ramp (1→5→10→20 concurrent) → score → Redis publish
"""

import time
import json
import threading
import statistics
import subprocess
import urllib.request
import urllib.error

try:
    import redis

    REDIS = redis.Redis(decode_responses=True)
    REDIS.ping()
    USE_REDIS = True
except Exception:
    USE_REDIS = False

API = "http://localhost:1234/v1/chat/completions"
MODEL = "qwen/qwen3.5-9b"
STAGES = [1, 5, 10, 20]


def get_vram():
    r = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader"],
        capture_output=True,
        text=True,
    )
    return {
        int(l.split(",")[0].strip()): int(l.split(",")[1].strip().split()[0])
        for l in r.stdout.strip().split("\n")
        if l
    }


def infer_one(prompt, timeout=45):
    data = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 60,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        API, data=data, headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
        elapsed = time.time() - t0
        tokens = resp.get("usage", {}).get("completion_tokens", 0)
        return {
            "ok": True,
            "latency": elapsed,
            "tokens": tokens,
            "tps": tokens / elapsed if elapsed > 0 else 0,
        }
    except Exception as e:
        return {
            "ok": False,
            "latency": time.time() - t0,
            "tokens": 0,
            "tps": 0,
            "err": str(e),
        }


def run_stage(n_concurrent, stage_name):
    print(f"\n[STAGE {stage_name}] {n_concurrent} requêtes concurrentes...")
    prompts = [f"Donne un fait scientifique #{i}" for i in range(n_concurrent)]
    results = [None] * n_concurrent
    threads = []

    def worker(i, p):
        results[i] = infer_one(p)

    t_start = time.time()
    for i, p in enumerate(prompts):
        t = threading.Thread(target=worker, args=(i, p))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    elapsed_total = time.time() - t_start

    ok = [r for r in results if r and r["ok"]]
    fail = n_concurrent - len(ok)
    tps_list = [r["tps"] for r in ok]
    lat_list = [r["latency"] for r in ok]

    stage_result = {
        "stage": stage_name,
        "concurrent": n_concurrent,
        "ok": len(ok),
        "fail": fail,
        "total_time_s": round(elapsed_total, 2),
        "avg_tps": round(statistics.mean(tps_list), 1) if tps_list else 0,
        "max_tps": round(max(tps_list), 1) if tps_list else 0,
        "avg_lat": round(statistics.mean(lat_list), 2) if lat_list else 0,
        "throughput_rps": round(len(ok) / elapsed_total, 2) if elapsed_total > 0 else 0,
        "vram_gpu4_mb": get_vram().get(4, 0),
    }
    print(
        f"  ✅ {len(ok)}/{n_concurrent} OK | avg {stage_result['avg_tps']} tok/s "
        f"| {stage_result['throughput_rps']} rps | VRAM GPU4: {stage_result['vram_gpu4_mb']} MiB"
    )
    return stage_result


# ─── MAIN ─────────────────────────────────────────────────────────────────────
print("=" * 60)
print("DOMINO PIPELINE — CUDA12 RTX 3080 Validation")
print("=" * 60)

# Probe
print("\n[PROBE] Vérification API...")
probe = infer_one("ping", timeout=10)
if not probe["ok"]:
    print(f"  ❌ API non disponible: {probe.get('err')}")
    exit(1)
print(f"  ✅ API OK — {probe['tps']:.1f} tok/s")

# Warmup
print("\n[WARMUP] 3 requêtes warmup...")
for _ in range(3):
    infer_one("warmup")
print("  ✅ Warmup done")

# Ramp stages
all_stages = []
for n in STAGES:
    stage = run_stage(n, f"x{n}")
    all_stages.append(stage)
    time.sleep(2)  # pause entre stages

# Score final
avg_tps_all = statistics.mean([s["avg_tps"] for s in all_stages if s["avg_tps"] > 0])
peak_tps = max(s["max_tps"] for s in all_stages)
peak_rps = max(s["throughput_rps"] for s in all_stages)
success_rate = statistics.mean([s["ok"] / s["concurrent"] * 100 for s in all_stages])

report = {
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "backend": "llama.cpp-linux-x86_64-nvidia-cuda12-avx2",
    "model": MODEL,
    "gpu": "RTX 3080 (GPU4, 10GB)",
    "stages": all_stages,
    "summary": {
        "avg_tps": round(avg_tps_all, 1),
        "peak_tps": round(peak_tps, 1),
        "peak_rps": round(peak_rps, 2),
        "success_rate_pct": round(success_rate, 1),
        "score": round(avg_tps_all * (success_rate / 100), 1),
    },
}

print("\n" + "=" * 60)
print("RAPPORT FINAL")
print("=" * 60)
print(f"  Backend  : {report['backend']}")
print(f"  GPU      : {report['gpu']}")
print(f"  Avg tok/s: {report['summary']['avg_tps']}")
print(f"  Peak tok/s: {report['summary']['peak_tps']}")
print(f"  Peak RPS : {report['summary']['peak_rps']}")
print(f"  Succès   : {report['summary']['success_rate_pct']}%")
print(f"  SCORE    : {report['summary']['score']}")

# Sauvegarder
out_path = f"/home/turbo/IA/Core/jarvis/exports/cuda12_bench_{time.strftime('%Y%m%d_%H%M%S')}.json"
with open(out_path, "w") as f:
    json.dump(report, f, indent=2)
print(f"\n  Rapport: {out_path}")

# Publier dans Redis
if USE_REDIS:
    REDIS.hset(
        "jarvis:m1:lmstudio",
        mapping={
            "backend": "cuda12-avx2",
            "gpu": "RTX3080",
            "score_tps": str(report["summary"]["avg_tps"]),
            "peak_tps": str(report["summary"]["peak_tps"]),
            "validated_at": report["timestamp"],
            "status": "CUDA_OK",
        },
    )
    REDIS.publish(
        "jarvis:events",
        json.dumps(
            {
                "event": "cuda_validated",
                "node": "M1",
                "score": report["summary"]["score"],
                "gpu": "RTX3080",
            }
        ),
    )
    print("  Redis: publié ✅")
else:
    print("  Redis: non disponible (skip)")

print("\n🏆 PIPELINE COMPLET")
