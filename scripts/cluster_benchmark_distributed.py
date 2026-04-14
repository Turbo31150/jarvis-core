#!/usr/bin/env python3
"""
cluster_benchmark_distributed — Benchmark M1 vs M2 vs M3
Tests latency, throughput, concurrency. Routing recommendations.
"""

import asyncio
import json
import time
import sys
from dataclasses import dataclass, field

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    import urllib.request

# ─── Cluster topology ───────────────────────────────────────────────────────
NODES = {
    "m1": {
        "url": "http://127.0.0.1:1234",
        "kind": "lmstudio",
        "role": "compute",
        "gpus": ["RTX_2060_12G", "RTX_3080_10G", "1660S_6G×4"],
        "vram_gb": 46,
    },
    "m2": {
        "url": "http://192.168.1.26:1234",
        "kind": "lmstudio",
        "role": "cache",
        "gpus": ["P4000_8G×8"],
        "vram_gb": 64,
    },
    "ol1": {
        "url": "http://127.0.0.1:11434",
        "kind": "ollama",
        "role": "buffer",
        "gpus": ["CPU/RAM"],
        "vram_gb": 0,
    },
}

TEST_PROMPT = "List 5 GPU models with their VRAM. Be brief."
TEST_PROMPTS = [
    ("short",  "Say 'OK' only."),
    ("medium", "Explain inference in 2 sentences."),
    ("long",   "Write a 10-step guide to GPU cluster optimization."),
]

@dataclass
class BenchResult:
    node: str
    model: str
    prompt_tag: str
    ttft_ms: float
    total_ms: float
    tokens: int
    tps: float
    error: str = ""

    def ok(self) -> bool:
        return not self.error

    def row(self) -> str:
        if self.error:
            return f"  {self.node:<4} {self.model:<30} {self.prompt_tag:<8} ERROR: {self.error}"
        return (f"  {self.node:<4} {self.model:<30} {self.prompt_tag:<8} "
                f"ttft={self.ttft_ms:>6.0f}ms total={self.total_ms:>7.0f}ms "
                f"tok={self.tokens:>4} tps={self.tps:>6.1f}")


def _call_lmstudio_sync(url: str, model: str, prompt: str) -> dict:
    import urllib.request as req
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 150,
        "temperature": 0.1,
        "stream": False,
    }).encode()
    r = req.urlopen(
        req.Request(f"{url}/v1/chat/completions", data=payload,
                    headers={"Content-Type": "application/json"}),
        timeout=30,
    )
    return json.loads(r.read())


def _call_ollama_sync(url: str, model: str, prompt: str) -> dict:
    import urllib.request as req
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "options": {"num_predict": 150},
        "stream": False,
    }).encode()
    r = req.urlopen(
        req.Request(f"{url}/api/generate", data=payload,
                    headers={"Content-Type": "application/json"}),
        timeout=30,
    )
    return json.loads(r.read())


async def bench_one(node: str, model: str, prompt_tag: str, prompt: str) -> BenchResult:
    cfg = NODES[node]
    loop = asyncio.get_event_loop()
    t0 = time.time()
    try:
        if cfg["kind"] == "lmstudio":
            data = await loop.run_in_executor(
                None, _call_lmstudio_sync, cfg["url"], model, prompt
            )
            ttft = data.get("usage", {}).get("prompt_eval_duration_ms", 0) or 0
            tokens = data.get("usage", {}).get("completion_tokens", 0) or 20
            total_ms = (time.time() - t0) * 1000
            tps = tokens / max((total_ms - ttft) / 1000, 0.001)
        else:  # ollama
            data = await loop.run_in_executor(
                None, _call_ollama_sync, cfg["url"], model, prompt
            )
            total_ms = (time.time() - t0) * 1000
            tokens = data.get("eval_count", 20)
            ttft = 0.0
            tps = tokens / max(total_ms / 1000, 0.001)

        return BenchResult(node, model, prompt_tag, ttft, total_ms, tokens, tps)
    except Exception as e:
        return BenchResult(node, model, prompt_tag, 0, 0, 0, 0, error=str(e)[:60])


async def get_models(node: str) -> list[str]:
    cfg = NODES[node]
    loop = asyncio.get_event_loop()
    try:
        if cfg["kind"] == "lmstudio":
            import urllib.request as req
            data = await loop.run_in_executor(
                None, lambda: json.loads(
                    req.urlopen(req.Request(f"{cfg['url']}/v1/models"), timeout=3).read()
                )
            )
            return [m["id"] for m in data.get("data", [])]
        elif cfg["kind"] == "ollama":
            import urllib.request as req
            data = await loop.run_in_executor(
                None, lambda: json.loads(
                    req.urlopen(req.Request(f"{cfg['url']}/api/tags"), timeout=3).read()
                )
            )
            return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        return []
    return []


async def cmd_inventory():
    print("\n╔═══════════════════════════════════════════════════╗")
    print("║        CLUSTER INVENTORY — M1 / M2 / M3          ║")
    print("╚═══════════════════════════════════════════════════╝\n")
    for node, cfg in NODES.items():
        models = await get_models(node)
        status = "✅ UP" if models else "❌ DOWN"
        print(f"  {node.upper()} ({cfg['role']}) {status}  VRAM={cfg['vram_gb']}GB")
        print(f"    URL: {cfg['url']}")
        print(f"    GPUs: {', '.join(cfg['gpus'])}")
        for m in models[:6]:
            print(f"    • {m}")
        if not models:
            print("    (no models / unreachable)")
        print()


async def cmd_benchmark(quick: bool = False):
    print("\n╔═══════════════════════════════════════════════════╗")
    print("║       DISTRIBUTED BENCHMARK — LATENCY + TPS      ║")
    print("╚═══════════════════════════════════════════════════╝\n")

    # Pick test models (shared models)
    m1_models = await get_models("m1")
    m2_models = await get_models("m2")
    
    # Find common models between M1 and M2 for fair comparison
    common = [m for m in m1_models if any(m.split("/")[-1] in m2 for m2 in m2_models)]
    if not common:
        # fallback: use qwen3.5-9b if available on both
        common = []
        for m in m1_models:
            if "qwen3.5-9b" in m or "qwen" in m:
                common.append(m)
                break
    
    test_models_m1 = common[:1] or m1_models[:1]
    test_models_m2 = m2_models[:1] if m2_models else []
    
    prompts = TEST_PROMPTS[:2] if quick else TEST_PROMPTS
    tasks = []
    
    for model in test_models_m1:
        for tag, prompt in prompts:
            tasks.append(bench_one("m1", model, tag, prompt))
    
    for model in test_models_m2:
        for tag, prompt in prompts:
            tasks.append(bench_one("m2", model, tag, prompt))

    results = await asyncio.gather(*tasks)
    
    print(f"  {'NODE':<4} {'MODEL':<30} {'PROMPT':<8} {'TTFT':>8} {'TOTAL':>9} {'TOK':>5} {'TPS':>7}")
    print("  " + "─" * 80)
    for r in results:
        print(r.row())
    
    # Summary
    ok_results = [r for r in results if r.ok()]
    if ok_results:
        m1_r = [r for r in ok_results if r.node == "m1"]
        m2_r = [r for r in ok_results if r.node == "m2"]
        print("\n  ── Summary ──")
        if m1_r:
            avg_tps_m1 = sum(r.tps for r in m1_r) / len(m1_r)
            avg_lat_m1 = sum(r.total_ms for r in m1_r) / len(m1_r)
            print(f"  M1: avg_latency={avg_lat_m1:.0f}ms  avg_tps={avg_tps_m1:.1f}")
        if m2_r:
            avg_tps_m2 = sum(r.tps for r in m2_r) / len(m2_r)
            avg_lat_m2 = sum(r.total_ms for r in m2_r) / len(m2_r)
            print(f"  M2: avg_latency={avg_lat_m2:.0f}ms  avg_tps={avg_tps_m2:.1f}")
    
    return results


async def cmd_concurrency(node: str = "m2", n: int = 4):
    print(f"\n╔═══════════════════════════════════════════════════╗")
    print(f"║       CONCURRENCY TEST — {node.upper()} (n={n})               ║")
    print(f"╚═══════════════════════════════════════════════════╝\n")
    models = await get_models(node)
    if not models:
        print(f"  {node} unreachable")
        return
    model = models[0]
    print(f"  Model: {model}")
    t0 = time.time()
    tasks = [bench_one(node, model, f"c{i}", TEST_PROMPT) for i in range(n)]
    results = await asyncio.gather(*tasks)
    wall = (time.time() - t0) * 1000
    ok = [r for r in results if r.ok()]
    print(f"  {n} concurrent requests → {len(ok)}/{n} OK  wall={wall:.0f}ms")
    for r in results:
        print(r.row())


async def cmd_routing():
    print("\n╔═══════════════════════════════════════════════════╗")
    print("║       ROUTING RECOMMENDATIONS                     ║")
    print("╚═══════════════════════════════════════════════════╝\n")
    print("""  Règles de routing recommandées pour JARVIS :

  ┌─────────────────────────────────────────────────────────┐
  │  Tâche                    → Nœud cible    Raison        │
  ├─────────────────────────────────────────────────────────┤
  │  Modèles > 20B (35b, r1)  → M2           64GB P4000    │
  │  Fast inference < 10B     → M1           RTX3080/2060  │
  │  Coding / coder models    → M1           2060 12G fast  │
  │  Embedding / nomic        → M2           stable        │
  │  Petites tâches / buffer  → OL1/M3       RAM 46G       │
  │  Batch overnight          → M2           8 P4000 stables│
  └─────────────────────────────────────────────────────────┘

  LM Link (config) :
  ─────────────────
  Sur M2 (Windows LM Studio) :
    Settings → LM Link → "Enable as inference server"
    → copier le token affiché

  Sur M1 (Linux LM Studio) :
    Settings → LM Link → "Add remote node"
    → IP: 192.168.1.26  Token: [coller]
    → Test connection

  Une fois connecté : M1 envoie les layers lourds à M2
  M2 garde les poids, M1 fait attention/compute
  VRAM effective fusionnée : ~110GB

  M3 (buffer) :
  ─────────────
  Ollama + Mistral 7B pour tâches légères
  RAM 46GB = modèles > VRAM en swap sans crash
""")


async def cmd_lmlink_test():
    """Test si LM Link fonctionne via modèle offloadé."""
    print("\n╔═══════════════════════════════════════════════════╗")
    print("║       LM LINK TEST                                ║")
    print("╚═══════════════════════════════════════════════════╝\n")
    # After LM Link is configured, a model loaded via LM Link appears
    # with a special tag in LM Studio - we check for it
    models = await get_models("m1")
    lmlink_models = [m for m in models if "lmlink" in m.lower() or "remote" in m.lower()]
    if lmlink_models:
        print(f"  ✅ LM Link models detected: {lmlink_models}")
        # bench one
        r = await bench_one("m1", lmlink_models[0], "lmlink", TEST_PROMPT)
        print(r.row())
    else:
        print("  ⚠ Aucun modèle LM Link détecté sur M1.")
        print("  → Configurer LM Link d'abord (voir 'routing')")
        print(f"  Modèles M1 actuels: {models}")


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    
    if cmd == "inventory":
        await cmd_inventory()
    elif cmd == "bench":
        await cmd_benchmark(quick="--quick" in sys.argv)
    elif cmd == "concurrency":
        node = sys.argv[2] if len(sys.argv) > 2 else "m2"
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        await cmd_concurrency(node, n)
    elif cmd == "routing":
        await cmd_routing()
    elif cmd == "lmlink":
        await cmd_lmlink_test()
    elif cmd == "full":
        await cmd_inventory()
        await cmd_benchmark(quick=True)
        await cmd_routing()
    else:
        print("""
cluster_benchmark_distributed.py — Audit M1/M2/M3

  python3 cluster_benchmark_distributed.py inventory       — inventaire nodes + models
  python3 cluster_benchmark_distributed.py bench           — benchmark latence/TPS
  python3 cluster_benchmark_distributed.py bench --quick   — version rapide
  python3 cluster_benchmark_distributed.py concurrency m2 4 — test 4 requêtes parallèles sur M2
  python3 cluster_benchmark_distributed.py routing          — guide config LM Link + routing
  python3 cluster_benchmark_distributed.py lmlink           — tester si LM Link actif
  python3 cluster_benchmark_distributed.py full             — tout en séquence
""")


if __name__ == "__main__":
    asyncio.run(main())
