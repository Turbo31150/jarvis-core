#!/usr/bin/env python3
"""
jarvis_cluster_rpc — llama.cpp RPC distributed inference across M1+M2
Manages RPC workers, model sharding, tensor distribution
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.cluster_rpc")

# ── Config ──────────────────────────────────────────────────────────────────

NODES: dict[str, dict] = {
    "M1": {
        "host": "127.0.0.1",
        "lmstudio_port": 1234,
        "rpc_port": 50052,
        "gpus": [0, 1, 2, 3, 4],  # physical GPU indices
        "vram_gb": [12, 6, 6, 6, 10],
        "primary": True,
    },
    "M2": {
        "host": "192.168.1.26",
        "lmstudio_port": 1234,
        "rpc_port": 50052,
        "gpus": [0, 1, 2],  # 3×8GB on M2 Windows
        "vram_gb": [8, 8, 8],
        "primary": False,
    },
}

REDIS_KEY = "jarvis:cluster_rpc"


@dataclass
class NodeStatus:
    name: str
    host: str
    lmstudio_ok: bool = False
    rpc_ok: bool = False
    models: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    last_check: float = 0.0


class ClusterRPCManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.statuses: dict[str, NodeStatus] = {}
        for name, cfg in NODES.items():
            self.statuses[name] = NodeStatus(name=name, host=cfg["host"])

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    # ── Health ───────────────────────────────────────────────────────────────

    async def check_node(self, name: str) -> NodeStatus:
        cfg = NODES[name]
        status = self.statuses[name]
        t0 = time.time()

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        ) as sess:
            # LM Studio API
            try:
                async with sess.get(
                    f"http://{cfg['host']}:{cfg['lmstudio_port']}/v1/models"
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        status.lmstudio_ok = True
                        status.models = [m["id"] for m in data.get("data", [])]
                    else:
                        status.lmstudio_ok = False
            except Exception:
                status.lmstudio_ok = False

        status.latency_ms = round((time.time() - t0) * 1000, 1)
        status.last_check = time.time()

        # RPC port check (TCP)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(cfg["host"], cfg["rpc_port"]),
                timeout=2,
            )
            writer.close()
            await writer.wait_closed()
            status.rpc_ok = True
        except Exception:
            status.rpc_ok = False

        return status

    async def check_all(self) -> dict[str, NodeStatus]:
        tasks = [self.check_node(n) for n in NODES]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, name in enumerate(NODES):
            if not isinstance(results[i], Exception):
                self.statuses[name] = results[i]
        return self.statuses

    # ── Model routing ─────────────────────────────────────────────────────────

    def find_model(self, model_id: str) -> str | None:
        """Return node name that has model_id loaded."""
        for name, status in self.statuses.items():
            if any(model_id.lower() in m.lower() for m in status.models):
                return name
        return None

    def best_node_for_inference(self) -> str:
        """Return node name with lowest latency and LMStudio OK."""
        candidates = [(n, s) for n, s in self.statuses.items() if s.lmstudio_ok]
        if not candidates:
            return "M1"
        return min(candidates, key=lambda x: x[1].latency_ms)[0]

    # ── RPC server launcher (M1 side) ─────────────────────────────────────────

    @staticmethod
    def launch_rpc_server(gpu_id: int, port: int = 50052) -> subprocess.Popen:
        """Launch llama-rpc-server on specific GPU for distributed compute."""
        # ik_llama.cpp has rpc server at /opt/ik_llama.cpp/build/bin/
        cmd = [
            "/opt/ik_llama.cpp/build/bin/llama-rpc-server",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--device",
            f"CUDA{gpu_id}",
        ]
        log.info(f"Launching RPC server GPU{gpu_id} on port {port}")
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # ── Tensor split calculator ───────────────────────────────────────────────

    @staticmethod
    def compute_tensor_split(
        model_size_gb: float, nodes: list[str] | None = None
    ) -> dict[str, list[float]]:
        """
        Compute optimal tensor split ratios for llama-server --tensor-split.
        Returns proportional split across available VRAM.
        """
        active_nodes = nodes or list(NODES.keys())
        all_vram: list[tuple[str, int, float]] = []  # (node, gpu_idx, vram_gb)

        for n in active_nodes:
            cfg = NODES[n]
            for i, vram in enumerate(cfg["vram_gb"]):
                all_vram.append((n, i, vram))

        total_vram = sum(v for _, _, v in all_vram)
        ratios = [round(v / total_vram * 100, 1) for _, _, v in all_vram]

        return {
            "nodes": active_nodes,
            "vram_map": all_vram,
            "ratios": ratios,
            "tensor_split_arg": ",".join(str(r) for r in ratios),
            "total_vram_gb": total_vram,
            "model_fits": total_vram >= model_size_gb * 1.1,
        }

    # ── Status report ─────────────────────────────────────────────────────────

    async def report(self) -> dict[str, Any]:
        await self.check_all()
        result = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes": {},
        }
        for name, status in self.statuses.items():
            result["nodes"][name] = {
                "host": status.host,
                "lmstudio": status.lmstudio_ok,
                "rpc": status.rpc_ok,
                "models": status.models[:5],
                "latency_ms": status.latency_ms,
            }

        if self.redis:
            await self.redis.set(
                REDIS_KEY,
                json.dumps(result),
                ex=60,
            )

        return result


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    mgr = ClusterRPCManager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "report":
        r = await mgr.report()
        print(json.dumps(r, indent=2))

    elif cmd == "find" and len(sys.argv) > 2:
        await mgr.check_all()
        node = mgr.find_model(sys.argv[2])
        print(f"Model '{sys.argv[2]}' → {node or 'NOT FOUND'}")

    elif cmd == "split" and len(sys.argv) > 2:
        model_gb = float(sys.argv[2])
        result = mgr.compute_tensor_split(model_gb)
        print(json.dumps(result, indent=2))

    elif cmd == "best":
        await mgr.check_all()
        print(mgr.best_node_for_inference())

    elif cmd == "rpc-launch":
        gpu_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        port = int(sys.argv[3]) if len(sys.argv) > 3 else 50052
        proc = mgr.launch_rpc_server(gpu_id, port)
        print(f"RPC server PID={proc.pid} GPU{gpu_id}:{port}")


if __name__ == "__main__":
    asyncio.run(main())
