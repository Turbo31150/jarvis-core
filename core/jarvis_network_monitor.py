#!/usr/bin/env python3
"""
jarvis_network_monitor — Cluster network health: latency, bandwidth, connectivity
Monitors M1↔M2↔OL1 links, detects degradation, measures throughput
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.network_monitor")

REDIS_KEY = "jarvis:network"
CHECK_INTERVAL = 60  # seconds

ENDPOINTS = {
    "M2_LMStudio": {"host": "192.168.1.26", "port": 1234, "type": "http"},
    "M2_SSH": {"host": "192.168.1.26", "port": 22, "type": "tcp"},
    "M2_RPC": {"host": "192.168.1.26", "port": 50052, "type": "tcp"},
    "OL1_Ollama": {"host": "127.0.0.1", "port": 11434, "type": "http"},
    "Redis": {"host": "127.0.0.1", "port": 6379, "type": "tcp"},
    "LMStudio_M1": {"host": "127.0.0.1", "port": 1234, "type": "http"},
}


@dataclass
class LinkStats:
    name: str
    host: str
    port: int
    reachable: bool = False
    latency_ms: float = 9999.0
    samples: list[float] = field(default_factory=list)
    consecutive_failures: int = 0
    last_check: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if not self.samples:
            return 9999.0
        return round(sum(self.samples[-20:]) / len(self.samples[-20:]), 1)

    @property
    def jitter_ms(self) -> float:
        s = self.samples[-10:]
        if len(s) < 2:
            return 0.0
        diffs = [abs(s[i] - s[i - 1]) for i in range(1, len(s))]
        return round(sum(diffs) / len(diffs), 1)

    @property
    def quality(self) -> str:
        if not self.reachable:
            return "DOWN"
        avg = self.avg_latency_ms
        if avg < 5:
            return "EXCELLENT"
        if avg < 20:
            return "GOOD"
        if avg < 100:
            return "FAIR"
        return "POOR"


async def measure_tcp_latency(
    host: str, port: int, timeout: float = 3.0
) -> tuple[bool, float]:
    t0 = time.time()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        ms = (time.time() - t0) * 1000
        writer.close()
        await writer.wait_closed()
        return True, round(ms, 2)
    except Exception:
        return False, round((time.time() - t0) * 1000, 2)


async def measure_http_latency(
    host: str, port: int, timeout: float = 5.0
) -> tuple[bool, float]:
    url = (
        f"http://{host}:{port}/v1/models"
        if port != 11434
        else f"http://{host}:{port}/api/tags"
    )
    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as sess:
            async with sess.get(url) as r:
                ms = (time.time() - t0) * 1000
                return r.status < 500, round(ms, 2)
    except Exception:
        return False, round((time.time() - t0) * 1000, 2)


def ping_latency(host: str, count: int = 3) -> tuple[float, float]:
    """ICMP ping — returns (avg_ms, packet_loss_pct)."""
    try:
        r = subprocess.run(
            ["ping", "-c", str(count), "-W", "2", host],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in r.stdout.split("\n"):
            if "rtt min/avg/max" in line:
                parts = line.split("=")[1].strip().split("/")
                avg_ms = float(parts[1])
                return avg_ms, 0.0
        # Check packet loss
        for line in r.stdout.split("\n"):
            if "packet loss" in line:
                loss = float(line.split("%")[0].split()[-1])
                return 9999.0, loss
    except Exception:
        pass
    return 9999.0, 100.0


async def measure_bandwidth_approx(host: str, port: int) -> float:
    """Approximate bandwidth by downloading model list and measuring speed."""
    url = f"http://{host}:{port}/v1/models"
    t0 = time.time()
    total_bytes = 0
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as sess:
            async with sess.get(url) as r:
                data = await r.read()
                total_bytes = len(data)
        elapsed = time.time() - t0
        if elapsed > 0:
            return round(total_bytes / elapsed / 1024, 1)  # KB/s
    except Exception:
        pass
    return 0.0


class NetworkMonitor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.links: dict[str, LinkStats] = {
            name: LinkStats(name=name, host=cfg["host"], port=cfg["port"])
            for name, cfg in ENDPOINTS.items()
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def check_link(self, name: str) -> LinkStats:
        cfg = ENDPOINTS[name]
        link = self.links[name]

        if cfg["type"] == "http":
            ok, ms = await measure_http_latency(cfg["host"], cfg["port"])
        else:
            ok, ms = await measure_tcp_latency(cfg["host"], cfg["port"])

        link.reachable = ok
        link.latency_ms = ms
        link.last_check = time.time()

        if ok:
            link.samples.append(ms)
            if len(link.samples) > 100:
                link.samples = link.samples[-100:]
            link.consecutive_failures = 0
        else:
            link.consecutive_failures += 1

        return link

    async def check_all(self) -> dict[str, LinkStats]:
        tasks = [self.check_link(name) for name in ENDPOINTS]
        await asyncio.gather(*tasks, return_exceptions=True)
        return self.links

    async def report(self) -> dict:
        await self.check_all()

        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "links": {},
            "all_ok": all(l.reachable for l in self.links.values()),
        }

        for name, link in self.links.items():
            result["links"][name] = {
                "reachable": link.reachable,
                "latency_ms": link.latency_ms,
                "avg_latency_ms": link.avg_latency_ms,
                "jitter_ms": link.jitter_ms,
                "quality": link.quality,
                "consecutive_failures": link.consecutive_failures,
            }

        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=120)

            # Alert on new failures
            for name, link in self.links.items():
                if link.consecutive_failures == 2:
                    await self.redis.publish(
                        "jarvis:events",
                        json.dumps(
                            {
                                "event": "network_degraded",
                                "link": name,
                                "host": link.host,
                                "port": link.port,
                            }
                        ),
                    )

        return result

    async def run_loop(self):
        await self.connect_redis()
        log.info("Network monitor started")
        while True:
            try:
                r = await self.report()
                down = [n for n, l in r["links"].items() if not l["reachable"]]
                if down:
                    log.warning(f"Links DOWN: {down}")
            except Exception as e:
                log.error(f"Network monitor error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    mon = NetworkMonitor()
    await mon.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        r = await mon.report()
        print(
            f"{'Link':<24} {'Status':>7} {'Latency':>9} {'Avg':>7} {'Jitter':>8} {'Quality'}"
        )
        print("-" * 72)
        for name, info in r["links"].items():
            status = "✅ UP" if info["reachable"] else "❌ DOWN"
            print(
                f"{name:<24} {status:>7} {info['latency_ms']:>7.1f}ms "
                f"{info['avg_latency_ms']:>5.1f}ms {info['jitter_ms']:>6.1f}ms "
                f"  {info['quality']}"
            )

    elif cmd == "ping" and len(sys.argv) > 2:
        host = sys.argv[2]
        avg, loss = ping_latency(host)
        print(f"{host}: avg={avg:.1f}ms loss={loss:.0f}%")

    elif cmd == "bandwidth" and len(sys.argv) > 2:
        host, port = sys.argv[2].split(":")
        bw = await measure_bandwidth_approx(host, int(port))
        print(f"{host}:{port}: ~{bw:.1f} KB/s")

    elif cmd == "daemon":
        logging.basicConfig(level=logging.INFO)
        await mon.run_loop()


import sys

if __name__ == "__main__":
    asyncio.run(main())
