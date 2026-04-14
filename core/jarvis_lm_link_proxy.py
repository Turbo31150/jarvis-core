#!/usr/bin/env python3
"""
jarvis_lm_link_proxy — Proxy layer for LM Studio LM Link + direct API fallback
Routes requests to M2 remote models via LM Link (port 5555) or direct API
Also supports llama.cpp RPC orchestration
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.lm_link_proxy")

# LM Link protocol: M1 acts as client, connects to M2 LM Link server
LM_LINK_ENDPOINTS = {
    "M2_LMLINK": "http://192.168.1.26:5555",  # LM Link server on M2 (if enabled)
    "M2_DIRECT": "http://192.168.1.26:1234",  # LM Studio direct API
    "M1_LOCAL": "http://127.0.0.1:1234",  # Local fallback
}

# Models known to be on M2
M2_MODELS = [
    "qwen/qwen3.5-35b-a3b",
    "deepseek/deepseek-r1-0528-qwen3-8b",
    "zai-org/glm-4.7-flash",
    "openai/gpt-oss-20b",
    "nvidia/nemotron-3-nano",
]

REDIS_KEY = "jarvis:lm_link"


@dataclass
class ProxyRoute:
    model: str
    endpoint: str
    method: str  # lmlink | direct | local
    latency_ms: float = 0.0
    ok: bool = False


class LMLinkProxy:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._route_cache: dict[str, ProxyRoute] = {}
        self._lmlink_available: bool | None = None  # None = not tested

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _probe_lmlink(self) -> bool:
        """Check if M2 LM Link server is reachable on port 5555."""
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as sess:
                async with sess.get(f"{LM_LINK_ENDPOINTS['M2_LMLINK']}/v1/models") as r:
                    self._lmlink_available = r.status == 200
        except Exception:
            self._lmlink_available = False
        return self._lmlink_available

    async def _probe_m2_direct(self) -> tuple[bool, float]:
        t0 = time.time()
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4)
            ) as sess:
                async with sess.get(f"{LM_LINK_ENDPOINTS['M2_DIRECT']}/v1/models") as r:
                    ok = r.status == 200
                    return ok, round((time.time() - t0) * 1000, 1)
        except Exception:
            return False, round((time.time() - t0) * 1000, 1)

    async def resolve_route(self, model_id: str) -> ProxyRoute:
        """Determine best endpoint for a given model."""
        # Check cache (30s TTL)
        cached = self._route_cache.get(model_id)
        if cached and time.time() - 0 < 30:  # simplistic, use Redis TTL in prod
            return cached

        is_m2_model = any(
            m.lower() in model_id.lower() or model_id.lower() in m.lower()
            for m in M2_MODELS
        )

        if is_m2_model:
            # Try LM Link first
            if self._lmlink_available is None:
                await self._probe_lmlink()

            if self._lmlink_available:
                route = ProxyRoute(
                    model=model_id,
                    endpoint=LM_LINK_ENDPOINTS["M2_LMLINK"],
                    method="lmlink",
                    ok=True,
                )
            else:
                # Fall back to direct M2 API
                ok, latency = await self._probe_m2_direct()
                route = ProxyRoute(
                    model=model_id,
                    endpoint=LM_LINK_ENDPOINTS["M2_DIRECT"],
                    method="direct",
                    latency_ms=latency,
                    ok=ok,
                )
                if not ok:
                    route = ProxyRoute(
                        model=model_id,
                        endpoint=LM_LINK_ENDPOINTS["M1_LOCAL"],
                        method="local",
                        ok=True,
                    )
        else:
            route = ProxyRoute(
                model=model_id,
                endpoint=LM_LINK_ENDPOINTS["M1_LOCAL"],
                method="local",
                ok=True,
            )

        self._route_cache[model_id] = route
        return route

    async def infer(self, payload: dict) -> dict:
        model = payload.get("model", "qwen/qwen3.5-9b")
        route = await self.resolve_route(model)
        url = f"{route.endpoint}/v1/chat/completions"
        t0 = time.time()

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as sess:
                async with sess.post(url, json=payload) as r:
                    result = await r.json()
                    elapsed = time.time() - t0
                    result["_proxy"] = {
                        "method": route.method,
                        "endpoint": route.endpoint,
                        "latency_s": round(elapsed, 3),
                    }
                    return result
        except Exception as e:
            log.error(f"Proxy infer failed [{route.method}]: {e}")
            # Fallback to M1
            if route.method != "local":
                log.info("Falling back to M1 local")
                fallback_url = f"{LM_LINK_ENDPOINTS['M1_LOCAL']}/v1/chat/completions"
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=120)
                    ) as sess:
                        async with sess.post(fallback_url, json=payload) as r:
                            result = await r.json()
                            result["_proxy"] = {
                                "method": "local_fallback",
                                "endpoint": LM_LINK_ENDPOINTS["M1_LOCAL"],
                                "latency_s": round(time.time() - t0, 3),
                            }
                            return result
                except Exception as e2:
                    return {"error": str(e2), "_proxy": {"method": "failed"}}
            return {"error": str(e), "_proxy": {"method": route.method}}

    async def status(self) -> dict:
        lmlink_ok = await self._probe_lmlink()
        m2_ok, m2_lat = await self._probe_m2_direct()

        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "lm_link": {
                "available": lmlink_ok,
                "endpoint": LM_LINK_ENDPOINTS["M2_LMLINK"],
                "note": "Requires LM Link enabled on M2 LM Studio",
            },
            "m2_direct": {
                "available": m2_ok,
                "endpoint": LM_LINK_ENDPOINTS["M2_DIRECT"],
                "latency_ms": m2_lat,
            },
            "m1_local": {
                "available": True,
                "endpoint": LM_LINK_ENDPOINTS["M1_LOCAL"],
            },
            "m2_models": M2_MODELS,
            "recommended": "lmlink" if lmlink_ok else ("direct" if m2_ok else "local"),
        }

        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=60)

        return result


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    proxy = LMLinkProxy()
    await proxy.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        r = await proxy.status()
        print(json.dumps(r, indent=2))

    elif cmd == "route" and len(sys.argv) > 2:
        route = await proxy.resolve_route(sys.argv[2])
        print(f"Model: {route.model}")
        print(f"Method: {route.method}")
        print(f"Endpoint: {route.endpoint}")

    elif cmd == "infer" and len(sys.argv) > 2:
        prompt = sys.argv[2]
        model = sys.argv[3] if len(sys.argv) > 3 else "qwen/qwen3.5-9b"
        result = await proxy.infer(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100,
                "temperature": 0,
            }
        )
        proxy_info = result.get("_proxy", {})
        content = (
            result.get("choices", [{}])[0]
            .get("message", {})
            .get("content", result.get("error", ""))
        )
        print(f"[{proxy_info.get('method')}] {proxy_info.get('latency_s', 0):.2f}s")
        print(content[:300])

    elif cmd == "setup-m2":
        print("""
=== ACTIVATION LM LINK SUR M2 (Windows) ===

1. Ouvre LM Studio sur M2 (192.168.1.26)
2. Vas dans Settings → LM Link
3. Active "Enable LM Link"
4. Connecte-toi avec le même compte que M1
5. M2 apparaîtra dans la liste des devices LM Link

Une fois activé, relance: python3 jarvis_lm_link_proxy.py status

Alternative sans compte (direct API):
  M2 est déjà accessible via http://192.168.1.26:1234
  lm-ask.sh route automatiquement vers M2

Pour llama.cpp RPC (GPU distribué):
  Sur M2 Windows: llama-rpc-server.exe --host 0.0.0.0 --port 50052
  Sur M1: utilise jarvis_cluster_rpc.py
""")


if __name__ == "__main__":
    asyncio.run(main())
