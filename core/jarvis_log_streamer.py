#!/usr/bin/env python3
"""
jarvis_log_streamer — Real-time log streaming via Redis pub/sub
Aggregates logs from all JARVIS services, filters, forwards to subscribers
"""

import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.log_streamer")

REDIS_CHANNEL = "jarvis:logs"
REDIS_BUFFER_KEY = "jarvis:logs:buffer"
BUFFER_MAX = 5000
LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
RESET = "\033[0m"


@dataclass
class LogEntry:
    ts: float
    level: str
    service: str
    message: str
    extra: dict | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "level": self.level,
            "service": self.service,
            "message": self.message,
            "extra": self.extra,
        }

    def format_ansi(self) -> str:
        color = LEVEL_COLORS.get(self.level, "")
        ts_str = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"{color}[{ts_str}] [{self.level:<8}] [{self.service:<24}] {self.message}{RESET}"

    def format_plain(self) -> str:
        ts_str = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"[{ts_str}] [{self.level:<8}] [{self.service:<24}] {self.message}"


class JarvisLogHandler(logging.Handler):
    """Python logging handler that publishes to Redis."""

    def __init__(self, service: str, redis_url: str = "redis://localhost"):
        super().__init__()
        self.service = service
        self._redis: aioredis.Redis | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def emit(self, record: logging.LogRecord):
        entry = LogEntry(
            ts=record.created,
            level=record.levelname,
            service=self.service,
            message=self.format(record),
        )
        try:
            self._queue.put_nowait(entry.to_dict())
        except asyncio.QueueFull:
            pass

    async def _publisher(self):
        self._redis = aioredis.Redis(decode_responses=True)
        while True:
            try:
                entry = await self._queue.get()
                raw = json.dumps(entry)
                await self._redis.publish(REDIS_CHANNEL, raw)
                await self._redis.lpush(REDIS_BUFFER_KEY, raw)
                await self._redis.ltrim(REDIS_BUFFER_KEY, 0, BUFFER_MAX - 1)
            except Exception:
                await asyncio.sleep(1)

    def start(self):
        asyncio.create_task(self._publisher())


class LogStreamer:
    """Subscribes to Redis log channel and streams to stdout or file."""

    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._filters: list[dict] = []

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception as e:
            print(f"Redis unavailable: {e}", file=sys.stderr)
            self.redis = None

    def add_filter(
        self,
        level: str | None = None,
        service: str | None = None,
        pattern: str | None = None,
    ):
        f: dict = {}
        if level:
            f["level"] = level.upper()
        if service:
            f["service"] = service
        if pattern:
            f["pattern"] = re.compile(pattern, re.IGNORECASE)
        self._filters.append(f)

    def _matches(self, entry: dict) -> bool:
        if not self._filters:
            return True
        for f in self._filters:
            match = True
            if "level" in f:
                levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
                entry_idx = (
                    levels.index(entry.get("level", "DEBUG"))
                    if entry.get("level") in levels
                    else 0
                )
                filter_idx = levels.index(f["level"]) if f["level"] in levels else 0
                if entry_idx < filter_idx:
                    match = False
            if "service" in f and f["service"] not in entry.get("service", ""):
                match = False
            if "pattern" in f and not f["pattern"].search(entry.get("message", "")):
                match = False
            if match:
                return True
        return False

    async def tail(
        self,
        lines: int = 100,
        follow: bool = False,
        output_file: Path | None = None,
        use_color: bool = True,
    ):
        out = sys.stdout
        if output_file:
            out = open(output_file, "a")
            use_color = False

        try:
            # Show buffered history first
            if self.redis:
                buffered = await self.redis.lrange(REDIS_BUFFER_KEY, 0, lines - 1)
                for raw in reversed(buffered):
                    try:
                        d = json.loads(raw)
                        entry = LogEntry(
                            **{k: d[k] for k in ("ts", "level", "service", "message")}
                        )
                        entry.extra = d.get("extra")
                        if self._matches(d):
                            line = (
                                entry.format_ansi()
                                if use_color
                                else entry.format_plain()
                            )
                            print(line, file=out)
                    except Exception:
                        continue

            if not follow:
                return

            # Stream live
            if not self.redis:
                print("[ERROR] Redis unavailable — cannot stream", file=sys.stderr)
                return

            async with self.redis.pubsub() as ps:
                await ps.subscribe(REDIS_CHANNEL)
                async for msg in ps.listen():
                    if msg["type"] != "message":
                        continue
                    try:
                        d = json.loads(msg["data"])
                        entry = LogEntry(
                            **{k: d[k] for k in ("ts", "level", "service", "message")}
                        )
                        if self._matches(d):
                            line = (
                                entry.format_ansi()
                                if use_color
                                else entry.format_plain()
                            )
                            print(line, file=out, flush=True)
                    except Exception:
                        continue
        finally:
            if output_file and out != sys.stdout:
                out.close()

    async def publish(self, level: str, service: str, message: str, **extra):
        if not self.redis:
            return
        entry = LogEntry(
            ts=time.time(),
            level=level,
            service=service,
            message=message,
            extra=extra or None,
        )
        raw = json.dumps(entry.to_dict())
        await self.redis.publish(REDIS_CHANNEL, raw)
        await self.redis.lpush(REDIS_BUFFER_KEY, raw)
        await self.redis.ltrim(REDIS_BUFFER_KEY, 0, BUFFER_MAX - 1)

    async def search(self, pattern: str, last_n: int = 1000) -> list[dict]:
        if not self.redis:
            return []
        rx = re.compile(pattern, re.IGNORECASE)
        raw_list = await self.redis.lrange(REDIS_BUFFER_KEY, 0, last_n - 1)
        results = []
        for raw in raw_list:
            try:
                d = json.loads(raw)
                if rx.search(d.get("message", "")) or rx.search(d.get("service", "")):
                    results.append(d)
            except Exception:
                continue
        return results


# ── CLI ───────────────────────────────────────────────────────────────────────


async def main():
    streamer = LogStreamer()
    await streamer.connect_redis()

    args = sys.argv[1:]
    follow = "-f" in args or "--follow" in args
    level = next(
        (
            args[i + 1]
            for i, a in enumerate(args)
            if a in ("-l", "--level") and i + 1 < len(args)
        ),
        None,
    )
    service = next(
        (
            args[i + 1]
            for i, a in enumerate(args)
            if a in ("-s", "--service") and i + 1 < len(args)
        ),
        None,
    )
    pattern = next(
        (
            args[i + 1]
            for i, a in enumerate(args)
            if a in ("-p", "--pattern") and i + 1 < len(args)
        ),
        None,
    )
    lines = int(
        next(
            (
                args[i + 1]
                for i, a in enumerate(args)
                if a in ("-n",) and i + 1 < len(args)
            ),
            50,
        )
    )

    if "search" in args:
        idx = args.index("search")
        query = args[idx + 1] if idx + 1 < len(args) else ""
        results = await streamer.search(query)
        for d in results:
            entry = LogEntry(
                ts=d["ts"], level=d["level"], service=d["service"], message=d["message"]
            )
            print(entry.format_ansi())
        return

    if level:
        streamer.add_filter(level=level)
    if service:
        streamer.add_filter(service=service)
    if pattern:
        streamer.add_filter(pattern=pattern)

    await streamer.tail(lines=lines, follow=follow)


if __name__ == "__main__":
    asyncio.run(main())
