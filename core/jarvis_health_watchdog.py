#!/usr/bin/env python3
"""
jarvis_health_watchdog — Service health watchdog with auto-restart
Monitors systemd services, processes, ports — restarts failures automatically
"""

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.health_watchdog")

REDIS_KEY = "jarvis:watchdog"
CHECK_INTERVAL = 30  # seconds
MAX_RESTARTS = 3
RESTART_WINDOW = 300  # 5 min — reset restart count after this

# Services to monitor
WATCHED_SERVICES = [
    {
        "name": "redis",
        "type": "systemd",
        "unit": "redis-server.service",
        "critical": True,
    },
    {
        "name": "lmstudio",
        "type": "port",
        "host": "127.0.0.1",
        "port": 1234,
        "critical": True,
    },
    {
        "name": "jarvis-gpu-oc",
        "type": "systemd",
        "unit": "jarvis-gpu-oc.service",
        "critical": False,
    },
    {
        "name": "m2-lmstudio",
        "type": "port",
        "host": "192.168.1.26",
        "port": 1234,
        "critical": False,
    },
]


@dataclass
class ServiceState:
    name: str
    healthy: bool = True
    last_check: float = 0.0
    last_healthy: float = field(default_factory=time.time)
    restart_count: int = 0
    restart_window_start: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    error: str = ""

    @property
    def downtime_s(self) -> float:
        if self.healthy:
            return 0.0
        return time.time() - self.last_healthy


def check_systemd(unit: str) -> tuple[bool, str]:
    r = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
    )
    ok = r.stdout.strip() == "active"
    return ok, r.stdout.strip()


async def check_port(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True, "open"
    except asyncio.TimeoutError:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def restart_service(unit: str) -> bool:
    r = subprocess.run(
        ["systemctl", "restart", unit],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


class HealthWatchdog:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self.states: dict[str, ServiceState] = {
            svc["name"]: ServiceState(name=svc["name"]) for svc in WATCHED_SERVICES
        }
        self._running = False

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def check_one(self, svc: dict) -> ServiceState:
        state = self.states[svc["name"]]
        prev_healthy = state.healthy

        if svc["type"] == "systemd":
            ok, msg = check_systemd(svc["unit"])
            state.error = "" if ok else msg
        elif svc["type"] == "port":
            ok, msg = await check_port(svc["host"], svc["port"])
            state.error = "" if ok else msg
        else:
            ok, msg = True, "unknown type"

        state.healthy = ok
        state.last_check = time.time()

        if ok:
            state.consecutive_failures = 0
            state.last_healthy = time.time()
            # Reset restart window
            if time.time() - state.restart_window_start > RESTART_WINDOW:
                state.restart_count = 0
                state.restart_window_start = time.time()
        else:
            state.consecutive_failures += 1

        # Transition: healthy → unhealthy
        if prev_healthy and not ok:
            log.warning(f"[WATCHDOG] {svc['name']} went DOWN: {state.error}")
            if self.redis:
                await self.redis.publish(
                    "jarvis:events",
                    json.dumps(
                        {
                            "event": "service_down",
                            "service": svc["name"],
                            "error": state.error,
                            "critical": svc.get("critical", False),
                        }
                    ),
                )

        # Auto-restart if systemd and critical
        if (
            not ok
            and svc["type"] == "systemd"
            and svc.get("critical", False)
            and state.consecutive_failures >= 2
            and state.restart_count < MAX_RESTARTS
        ):
            log.warning(f"[WATCHDOG] Auto-restarting {svc['unit']}...")
            if restart_service(svc["unit"]):
                state.restart_count += 1
                log.info(
                    f"[WATCHDOG] Restarted {svc['unit']} (attempt {state.restart_count})"
                )
                if self.redis:
                    await self.redis.publish(
                        "jarvis:events",
                        json.dumps(
                            {
                                "event": "service_restarted",
                                "service": svc["name"],
                                "attempt": state.restart_count,
                            }
                        ),
                    )
            else:
                log.error(f"[WATCHDOG] Restart failed for {svc['unit']}")

        return state

    async def check_all(self) -> dict[str, ServiceState]:
        tasks = [self.check_one(svc) for svc in WATCHED_SERVICES]
        await asyncio.gather(*tasks, return_exceptions=True)
        return self.states

    async def report(self) -> dict:
        await self.check_all()
        result = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "services": {},
            "all_healthy": all(s.healthy for s in self.states.values()),
        }
        for name, state in self.states.items():
            result["services"][name] = {
                "healthy": state.healthy,
                "downtime_s": round(state.downtime_s, 1),
                "consecutive_failures": state.consecutive_failures,
                "restart_count": state.restart_count,
                "error": state.error,
            }
        if self.redis:
            await self.redis.set(REDIS_KEY, json.dumps(result), ex=60)
        return result

    async def run_loop(self):
        self._running = True
        await self.connect_redis()
        log.info("Health watchdog started")
        while self._running:
            try:
                r = await self.report()
                unhealthy = [n for n, s in r["services"].items() if not s["healthy"]]
                if unhealthy:
                    log.warning(f"Unhealthy: {unhealthy}")
            except Exception as e:
                log.error(f"Watchdog error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self):
        self._running = False


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    import sys

    dog = HealthWatchdog()
    await dog.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        r = await dog.report()
        ok_all = "✅" if r["all_healthy"] else "❌"
        print(f"Overall: {ok_all}\n")
        print(
            f"{'Service':<20} {'Status':>8} {'Failures':>9} {'Restarts':>9} {'Error'}"
        )
        print("-" * 70)
        for name, info in r["services"].items():
            status = "✅ UP" if info["healthy"] else "❌ DOWN"
            print(
                f"{name:<20} {status:>8} {info['consecutive_failures']:>9} "
                f"{info['restart_count']:>9}  {info['error'][:30]}"
            )

    elif cmd == "daemon":
        logging.basicConfig(level=logging.INFO)
        await dog.run_loop()

    elif cmd == "add" and len(sys.argv) >= 5:
        # Dynamic add (not persistent across restarts)
        stype = sys.argv[2]
        name = sys.argv[3]
        if stype == "port":
            host, port = sys.argv[4].split(":")
            WATCHED_SERVICES.append(
                {
                    "name": name,
                    "type": "port",
                    "host": host,
                    "port": int(port),
                    "critical": False,
                }
            )
        elif stype == "systemd":
            WATCHED_SERVICES.append(
                {
                    "name": name,
                    "type": "systemd",
                    "unit": sys.argv[4],
                    "critical": False,
                }
            )
        dog.states[name] = ServiceState(name=name)
        r = await dog.report()
        print(f"Added {name}: {r['services'].get(name, {})}")


if __name__ == "__main__":
    asyncio.run(main())
