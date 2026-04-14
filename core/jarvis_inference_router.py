#!/usr/bin/env python3
"""
jarvis_inference_router — Smart routing across M1+M2 cluster
Routes inference requests based on model availability, load, latency
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.inference_router")

NODES = {
    "M1": {"url": "http://127.0.0.1:1234", "priority": 1},
    "M2": {"url": "http://192.168.1.26:1234", "priority": 2},
    "OL1": {"url": "http://127.0.0.1:11434", "priority": 3},
}

REDIS_KEY = "jarvis:inference_router"
HEALTH_TTL = 30  # seconds before re-check


@dataclass
class NodeHealth:
    name: str
    url: str
    ok: bool = False
    latency_ms: float = 9999.0
    models: list[str] = field(default_factory=list)
    last_check: float = 0.0
    requests_total: int = 0
    requests_ok: int = 0

    @property
    def success_rate(self) -> float:
        if self.requests_total == 0:
            return 1.0
        return self.requests_ok / self.requests_total

    @property
    def score(self) -> float:
        """Lower is better: latency * (1 / success_rate)"""
        if not self.ok:
            return 99999.0
        return self.latency_ms * (1.0 / max(self.success_rate, 0.01))


class InferenceRouter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.health: dict[str, NodeHealth] = {
            n: NodeHealth(name=n, url=cfg["url"]) for n, cfg in NODES.items()
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    # ── Health probe ──────────────────────────────────────────────────────────

    async def probe_node(self, name: str) -> NodeHealth:
        h = self.health[name]
        url = NODES[name]["url"]
        t0 = time.time()

        is_ollama = "11434" in url
        models_endpoint = f"{url}/api/tags" if is_ollama else f"{url}/v1/models"

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as sess:
                async with sess.get(models_endpoint) as r:
                    if r.status == 200:
                        data = await r.json()
                        h.ok = True
                        if is_ollama:
                            h.models = [m["name"] for m in data.get("models", [])]
                        else:
                            h.models = [m["id"] for m in data.get("data", [])]
                    else:
                        h.ok = False
                        h.models = []
        except Exception:
            h.ok = False
            h.models = []

        h.latency_ms = round((time.time() - t0) * 1000, 1)
        h.last_check = time.time()
        return h

    async def refresh_all(self):
        tasks = [self.probe_node(n) for n in NODES]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def maybe_refresh(self, name: str):
        h = self.health[name]
        if time.time() - h.last_check > HEALTH_TTL:
            await self.probe_node(name)

    # ── Routing logic ─────────────────────────────────────────────────────────

    async def find_node_for_model(self, model_id: str) -> str | None:
        """Find which node has model_id available."""
        for name in sorted(NODES, key=lambda n: NODES[n]["priority"]):
            await self.maybe_refresh(name)
            h = self.health[name]
            if h.ok and any(model_id.lower() in m.lower() for m in h.models):
                return name
        return None

    async def best_node(self, model_id: str | None = None) -> str | None:
        """Return lowest-score healthy node, optionally filtered by model."""
        if model_id:
            return await self.find_node_for_model(model_id)

        await self.refresh_all()
        candidates = [(n, h) for n, h in self.health.items() if h.ok]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[1].score)[0]

    # ── Proxied inference ─────────────────────────────────────────────────────

    async def infer(
        self,
        payload: dict,
        model_id: str | None = None,
        node: str | None = None,
    ) -> dict:
        target = node or await self.best_node(model_id or payload.get("model"))
        if not target:
            return {"error": "no healthy node available"}

        h = self.health[target]
        url = f"{h.url}/v1/chat/completions"
        h.requests_total += 1
        t0 = time.time()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                async with sess.post(url, json=payload) as r:
                    result = await r.json()
                    elapsed = time.time() - t0
                    h.requests_ok += 1
                    result["_router"] = {
                        "node": target,
                        "latency_s": round(elapsed, 3),
                    }
                    return result
        except Exception as e:
            h.ok = False
            return {"error": str(e), "_router": {"node": target}}

    # ── Status ────────────────────────────────────────────────────────────────

    async def status(self) -> dict:
        await self.refresh_all()
        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes": {},
        }
        for name, h in self.health.items():
            result["nodes"][name] = {
                "ok": h.ok,
                "latency_ms": h.latency_ms,
                "models": h.models[:6],
                "model_count": len(h.models),
                "score": round(h.score, 1),
                "success_rate": round(h.success_rate, 3),
            }
        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=60)
        return result


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    router = InferenceRouter()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        r = await router.status()
        print(f"{'Node':<6} {'OK':>3} {'Latency':>9} {'Models':>7} {'Score':>8}")
        print("-" * 40)
        for name, info in r["nodes"].items():
            ok = "✅" if info["ok"] else "❌"
            print(
                f"{name:<6} {ok:>3} {info['latency_ms']:>7.0f}ms "
                f"{info['model_count']:>6} {info['score']:>8.1f}"
            )

    elif cmd == "find" and len(sys.argv) > 2:
        node = await router.find_node_for_model(sys.argv[2])
        print(f"'{sys.argv[2]}' → {node or 'NOT FOUND'}")

    elif cmd == "best":
        model = sys.argv[2] if len(sys.argv) > 2 else None
        node = await router.best_node(model)
        print(f"Best node{f' for {model}' if model else ''}: {node}")

    elif cmd == "infer" and len(sys.argv) > 2:
        prompt = sys.argv[2]
        model = sys.argv[3] if len(sys.argv) > 3 else "qwen/qwen3.5-9b"
        result = await router.infer(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100,
                "temperature": 0,
            }
        )
        node = result.get("_router", {}).get("node", "?")
        latency = result.get("_router", {}).get("latency_s", 0)
        content = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", result.get("error", ""))
        )
        print(f"[{node}] {latency:.2f}s → {content[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
