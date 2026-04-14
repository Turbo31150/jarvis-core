#!/usr/bin/env python3
"""
jarvis_log_shipper — Structured log collection and forwarding
Tails log files, parses structured logs, ships to Redis/file with filtering and batching
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.log_shipper")

REDIS_PREFIX = "jarvis:logs:"
SHIP_BUFFER_SIZE = 500
FLUSH_INTERVAL_S = 5.0


@dataclass
class LogEntry:
    level: str
    message: str
    source: str
    ts: float = field(default_factory=time.time)
    fields: dict = field(default_factory=dict)
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "message": self.message,
            "source": self.source,
            "ts": self.ts,
            "fields": self.fields,
        }


@dataclass
class ShipTarget:
    name: str
    target_type: str  # redis | file | stdout
    min_level: str = "INFO"
    filter_sources: list[str] = field(default_factory=list)  # empty = all
    settings: dict = field(default_factory=dict)


LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}

# Regex parsers for common log formats
LOG_PARSERS = [
    # Python logging: "2024-01-01 12:00:00,123 - name - LEVEL - message"
    re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,\.]?\d*) - ([\w\.]+) - (\w+) - (.+)"
    ),
    # Simple: "LEVEL: message"
    re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s*(.+)"),
    # JSON line
    None,  # handled separately
]


def _parse_line(line: str, source: str = "unknown") -> LogEntry:
    line = line.strip()
    if not line:
        return None

    # Try JSON
    if line.startswith("{"):
        try:
            d = json.loads(line)
            return LogEntry(
                level=d.get("level", d.get("lvl", "INFO")).upper(),
                message=d.get("message", d.get("msg", line)),
                source=d.get("source", d.get("name", source)),
                fields={
                    k: v
                    for k, v in d.items()
                    if k
                    not in (
                        "level",
                        "lvl",
                        "message",
                        "msg",
                        "source",
                        "name",
                        "ts",
                        "time",
                    )
                },
                raw=line,
            )
        except Exception:
            pass

    # Try pattern parsers
    for pattern in LOG_PARSERS[:2]:
        if pattern is None:
            continue
        m = pattern.match(line)
        if m:
            groups = m.groups()
            if len(groups) == 4:
                return LogEntry(
                    level=groups[2].upper(),
                    message=groups[3],
                    source=groups[1],
                    raw=line,
                )
            elif len(groups) == 2:
                return LogEntry(
                    level=groups[0].upper(), message=groups[1], source=source, raw=line
                )

    # Plain text fallback
    level = "INFO"
    for lvl in ("ERROR", "CRITICAL", "WARNING", "DEBUG"):
        if lvl in line.upper():
            level = lvl
            break
    return LogEntry(level=level, message=line, source=source, raw=line)


class LogShipper:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._targets: dict[str, ShipTarget] = {}
        self._buffer: list[LogEntry] = []
        self._tailed_files: dict[str, int] = {}  # path → last position
        self._flush_task: asyncio.Task | None = None
        self._stats: dict[str, int] = {
            "received": 0,
            "shipped": 0,
            "filtered": 0,
            "errors": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
            self._targets["redis_default"] = ShipTarget(
                name="redis_default", target_type="redis", min_level="INFO"
            )
        except Exception:
            self.redis = None

    def add_target(self, target: ShipTarget):
        self._targets[target.name] = target

    def ingest(
        self, level: str, message: str, source: str = "jarvis", **fields
    ) -> LogEntry:
        entry = LogEntry(
            level=level.upper(), message=message, source=source, fields=fields
        )
        self._buffer.append(entry)
        self._stats["received"] += 1
        if len(self._buffer) >= SHIP_BUFFER_SIZE:
            asyncio.create_task(self._flush())
        return entry

    def ingest_line(self, line: str, source: str = "unknown") -> LogEntry | None:
        entry = _parse_line(line, source)
        if entry:
            self._buffer.append(entry)
            self._stats["received"] += 1
        return entry

    def _level_passes(self, entry_level: str, min_level: str) -> bool:
        return LEVEL_ORDER.get(entry_level, 1) >= LEVEL_ORDER.get(min_level, 1)

    async def _flush(self):
        if not self._buffer:
            return
        batch = self._buffer[:]
        self._buffer.clear()

        for target in self._targets.values():
            filtered = [
                e
                for e in batch
                if self._level_passes(e.level, target.min_level)
                and (not target.filter_sources or e.source in target.filter_sources)
            ]
            self._stats["filtered"] += len(batch) - len(filtered)

            for entry in filtered:
                try:
                    await self._ship(entry, target)
                    self._stats["shipped"] += 1
                except Exception as e:
                    self._stats["errors"] += 1
                    log.debug(f"Ship error [{target.name}]: {e}")

    async def _ship(self, entry: LogEntry, target: ShipTarget):
        if target.target_type == "redis" and self.redis:
            key = f"{REDIS_PREFIX}{entry.source}"
            await self.redis.lpush(key, json.dumps(entry.to_dict()))
            await self.redis.ltrim(key, 0, 999)
            await self.redis.expire(key, 86400)
            # Global stream
            await self.redis.lpush(f"{REDIS_PREFIX}all", json.dumps(entry.to_dict()))
            await self.redis.ltrim(f"{REDIS_PREFIX}all", 0, 4999)

        elif target.target_type == "file":
            path = target.settings.get("path", "/tmp/jarvis_shipped.log")
            with open(path, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")

        elif target.target_type == "stdout":
            ts = time.strftime("%H:%M:%S", time.localtime(entry.ts))
            print(f"[{ts}] {entry.level:<8} {entry.source:<20} {entry.message}")

    async def tail_file(self, path: str, source: str = "", interval_s: float = 1.0):
        """Continuously tail a log file and ingest new lines."""
        p = Path(path)
        src = source or p.stem
        pos = self._tailed_files.get(path, 0)
        if pos == 0 and p.exists():
            pos = p.stat().st_size  # Start from end

        while True:
            try:
                if p.exists():
                    current_size = p.stat().st_size
                    if current_size > pos:
                        with open(path) as f:
                            f.seek(pos)
                            new_lines = f.read()
                        pos = current_size
                        self._tailed_files[path] = pos
                        for line in new_lines.splitlines():
                            self.ingest_line(line, source=src)
            except Exception as e:
                log.debug(f"Tail error [{path}]: {e}")
            await asyncio.sleep(interval_s)

    async def start_flush_loop(self):
        async def loop():
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await self._flush()

        self._flush_task = asyncio.create_task(loop())

    async def query(
        self, source: str = "all", level: str = "INFO", limit: int = 50
    ) -> list[dict]:
        if not self.redis:
            return []
        key = f"{REDIS_PREFIX}{source}"
        raw_entries = await self.redis.lrange(key, 0, limit - 1)
        result = []
        for raw in raw_entries:
            try:
                e = json.loads(raw)
                if self._level_passes(e.get("level", "INFO"), level):
                    result.append(e)
            except Exception:
                pass
        return result

    def stats(self) -> dict:
        return {
            **self._stats,
            "buffer_size": len(self._buffer),
            "targets": list(self._targets.keys()),
            "tailed_files": list(self._tailed_files.keys()),
        }


async def main():
    import sys

    shipper = LogShipper()
    await shipper.connect_redis()
    shipper.add_target(ShipTarget("stdout", "stdout", min_level="INFO"))
    await shipper.start_flush_loop()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        shipper.ingest("INFO", "System started", source="jarvis.core")
        shipper.ingest(
            "WARNING", "High memory usage: 87%", source="jarvis.monitor", ram_pct=87
        )
        shipper.ingest(
            "ERROR",
            "Model request failed: timeout",
            source="jarvis.llm",
            model="qwen3.5",
        )
        shipper.ingest("DEBUG", "Cache hit for key abc123", source="jarvis.cache")
        shipper.ingest(
            "INFO", "Request completed in 243ms", source="jarvis.api", latency_ms=243
        )

        # Parse raw log lines
        lines = [
            "2024-01-15 10:23:44,123 - jarvis.redis - INFO - Connected to Redis",
            '{"level": "WARNING", "message": "Queue depth: 500", "source": "jarvis.queue"}',
            "ERROR: Database connection lost",
        ]
        for line in lines:
            shipper.ingest_line(line)

        await asyncio.sleep(FLUSH_INTERVAL_S + 0.5)
        print(f"\nStats: {json.dumps(shipper.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(shipper.stats(), indent=2))

    elif cmd == "tail" and len(sys.argv) > 2:
        print(f"Tailing {sys.argv[2]}...")
        await shipper.tail_file(sys.argv[2])


if __name__ == "__main__":
    asyncio.run(main())
