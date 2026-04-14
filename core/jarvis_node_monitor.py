#!/usr/bin/env python3
"""
jarvis_node_monitor — Real-time node health monitoring for cluster nodes
Tracks CPU, RAM, GPU, latency and fires alerts on threshold violations
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.node_monitor")

REDIS_PREFIX = "jarvis:nodemon:"


class NodeStatus(str, Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class NodeThresholds:
    cpu_warn: float = 70.0
    cpu_crit: float = 90.0
    ram_warn: float = 80.0
    ram_crit: float = 95.0
    gpu_temp_warn: float = 75.0
    gpu_temp_crit: float = 85.0
    latency_warn_ms: float = 500.0
    latency_crit_ms: float = 2000.0
    offline_after_s: float = 60.0


@dataclass
class NodeMetrics:
    node_id: str
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    gpu_temp: float = 0.0
    gpu_vram_pct: float = 0.0
    latency_ms: float = 0.0
    models_loaded: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "cpu_pct": round(self.cpu_pct, 1),
            "ram_pct": round(self.ram_pct, 1),
            "gpu_temp": round(self.gpu_temp, 1),
            "gpu_vram_pct": round(self.gpu_vram_pct, 1),
            "latency_ms": round(self.latency_ms, 1),
            "models_loaded": self.models_loaded,
            "ts": self.ts,
        }


@dataclass
class NodeAlert:
    node_id: str
    metric: str
    value: float
    threshold: float
    severity: str  # "warn" | "crit"
    message: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "metric": self.metric,
            "value": round(self.value, 1),
            "threshold": self.threshold,
            "severity": self.severity,
            "message": self.message,
            "ts": self.ts,
        }


@dataclass
class NodeState:
    node_id: str
    url: str
    status: NodeStatus = NodeStatus.UNKNOWN
    thresholds: NodeThresholds = field(default_factory=NodeThresholds)
    latest: NodeMetrics | None = None
    history: list[NodeMetrics] = field(default_factory=list)
    consecutive_failures: int = 0
    last_seen: float = 0.0
    total_polls: int = 0
    total_alerts: int = 0

    @property
    def availability_pct(self) -> float:
        if self.total_polls == 0:
            return 100.0
        online = sum(1 for m in self.history if m.latency_ms < 2000)
        return online / self.total_polls * 100

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "url": self.url,
            "status": self.status.value,
            "availability_pct": round(self.availability_pct, 2),
            "consecutive_failures": self.consecutive_failures,
            "last_seen": self.last_seen,
            "total_alerts": self.total_alerts,
            "latest": self.latest.to_dict() if self.latest else None,
        }


async def _probe_lmstudio(url: str, timeout_s: float = 5.0) -> NodeMetrics | None:
    """Probe an LM Studio node via /v1/models."""
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.get(f"{url}/v1/models") as r:
                latency = (time.time() - t0) * 1000
                if r.status == 200:
                    data = await r.json(content_type=None)
                    models = [m["id"] for m in data.get("data", [])]
                    return NodeMetrics(
                        node_id=url,
                        latency_ms=latency,
                        models_loaded=models,
                    )
    except Exception:
        pass
    return None


class NodeMonitor:
    def __init__(self, poll_interval_s: float = 15.0):
        self.redis: aioredis.Redis | None = None
        self._nodes: dict[str, NodeState] = {}
        self._poll_interval = poll_interval_s
        self._running = False
        self._alert_callbacks: list[Callable] = []
        self._alerts: list[NodeAlert] = []
        self._stats: dict[str, int] = {
            "polls": 0,
            "failures": 0,
            "alerts_fired": 0,
            "recoveries": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_node(
        self,
        node_id: str,
        url: str,
        thresholds: NodeThresholds | None = None,
    ):
        self._nodes[node_id] = NodeState(
            node_id=node_id,
            url=url,
            thresholds=thresholds or NodeThresholds(),
        )

    def on_alert(self, callback: Callable):
        self._alert_callbacks.append(callback)

    def _check_thresholds(
        self, state: NodeState, metrics: NodeMetrics
    ) -> list[NodeAlert]:
        t = state.thresholds
        alerts = []

        checks = [
            ("cpu_pct", metrics.cpu_pct, t.cpu_warn, t.cpu_crit, "CPU usage"),
            ("ram_pct", metrics.ram_pct, t.ram_warn, t.ram_crit, "RAM usage"),
            (
                "gpu_temp",
                metrics.gpu_temp,
                t.gpu_temp_warn,
                t.gpu_temp_crit,
                "GPU temperature",
            ),
            (
                "latency_ms",
                metrics.latency_ms,
                t.latency_warn_ms,
                t.latency_crit_ms,
                "Latency",
            ),
        ]
        for metric, value, warn, crit, label in checks:
            if value == 0.0:
                continue  # not reported
            if value >= crit:
                alerts.append(
                    NodeAlert(
                        state.node_id,
                        metric,
                        value,
                        crit,
                        "crit",
                        f"{state.node_id} {label} critical: {value:.1f} >= {crit}",
                    )
                )
            elif value >= warn:
                alerts.append(
                    NodeAlert(
                        state.node_id,
                        metric,
                        value,
                        warn,
                        "warn",
                        f"{state.node_id} {label} warning: {value:.1f} >= {warn}",
                    )
                )
        return alerts

    def _update_status(self, state: NodeState, metrics: NodeMetrics | None):
        prev = state.status
        state.total_polls += 1

        if metrics is None:
            state.consecutive_failures += 1
            self._stats["failures"] += 1
            elapsed = time.time() - state.last_seen if state.last_seen else 9999
            if elapsed >= state.thresholds.offline_after_s:
                state.status = NodeStatus.OFFLINE
            else:
                state.status = NodeStatus.DEGRADED
            return

        state.consecutive_failures = 0
        state.last_seen = time.time()
        state.latest = metrics
        state.history.append(metrics)
        if len(state.history) > 100:
            state.history.pop(0)

        alerts = self._check_thresholds(state, metrics)
        if alerts:
            state.status = NodeStatus.DEGRADED
            for a in alerts:
                self._alerts.append(a)
                if len(self._alerts) > 500:
                    self._alerts.pop(0)
                state.total_alerts += 1
                self._stats["alerts_fired"] += 1
                for cb in self._alert_callbacks:
                    try:
                        cb(a)
                    except Exception:
                        pass
        else:
            state.status = NodeStatus.ONLINE

        if (
            prev in (NodeStatus.OFFLINE, NodeStatus.DEGRADED)
            and state.status == NodeStatus.ONLINE
        ):
            self._stats["recoveries"] += 1
            log.info(f"Node {state.node_id} recovered")

    async def poll_node(self, node_id: str):
        state = self._nodes.get(node_id)
        if not state:
            return
        self._stats["polls"] += 1
        metrics = await _probe_lmstudio(state.url)
        self._update_status(state, metrics)

        if self.redis:
            asyncio.create_task(
                self.redis.setex(
                    f"{REDIS_PREFIX}{node_id}",
                    120,
                    json.dumps(state.to_dict()),
                )
            )

    async def poll_all(self):
        await asyncio.gather(*[self.poll_node(nid) for nid in self._nodes])

    async def run(self):
        self._running = True
        while self._running:
            await self.poll_all()
            await asyncio.sleep(self._poll_interval)

    def stop(self):
        self._running = False

    def get_status(self, node_id: str) -> NodeStatus:
        state = self._nodes.get(node_id)
        return state.status if state else NodeStatus.UNKNOWN

    def online_nodes(self) -> list[str]:
        return [nid for nid, s in self._nodes.items() if s.status == NodeStatus.ONLINE]

    def all_states(self) -> list[dict]:
        return [s.to_dict() for s in self._nodes.values()]

    def recent_alerts(self, n: int = 10) -> list[dict]:
        return [a.to_dict() for a in self._alerts[-n:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "nodes": len(self._nodes),
            "online": len(self.online_nodes()),
        }


def build_jarvis_node_monitor() -> NodeMonitor:
    mon = NodeMonitor(poll_interval_s=15.0)
    mon.add_node(
        "m1",
        "http://192.168.1.85:1234",
        NodeThresholds(latency_warn_ms=300, latency_crit_ms=1500),
    )
    mon.add_node(
        "m2",
        "http://192.168.1.26:1234",
        NodeThresholds(latency_warn_ms=500, latency_crit_ms=2000),
    )
    mon.add_node(
        "ol1",
        "http://127.0.0.1:11434",
        NodeThresholds(latency_warn_ms=200, latency_crit_ms=1000),
    )

    def log_alert(alert: NodeAlert):
        sev = "⚠️" if alert.severity == "warn" else "🔴"
        log.warning(f"{sev} {alert.message}")

    mon.on_alert(log_alert)
    return mon


async def main():
    import sys

    mon = build_jarvis_node_monitor()
    await mon.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Polling all cluster nodes...")
        await mon.poll_all()
        print("\nNode states:")
        for s in mon.all_states():
            icon = {
                "online": "✅",
                "degraded": "⚠️",
                "offline": "❌",
                "unknown": "?",
            }.get(s["status"], "?")
            lat = s["latest"]["latency_ms"] if s["latest"] else "N/A"
            models = len(s["latest"]["models_loaded"]) if s["latest"] else 0
            print(
                f"  {icon} {s['node_id']:<6} {s['status']:<10} lat={lat}ms models={models}"
            )

        print(f"\nOnline: {mon.online_nodes()}")
        print(f"Stats: {json.dumps(mon.stats(), indent=2)}")

    elif cmd == "watch":
        print("Starting continuous monitoring (Ctrl+C to stop)...")
        try:
            await mon.run()
        except KeyboardInterrupt:
            mon.stop()

    elif cmd == "stats":
        print(json.dumps(mon.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
