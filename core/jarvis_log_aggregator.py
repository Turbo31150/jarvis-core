#!/usr/bin/env python3
"""
jarvis_log_aggregator — Multi-source log collection, parsing, and aggregation
Collects logs from files, Redis streams, and agents; applies structured parsing
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.log_aggregator")

REDIS_PREFIX = "jarvis:logs:"
REDIS_STREAM = "jarvis:logstream"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @classmethod
    def from_str(cls, s: str) -> "LogLevel":
        s = s.upper().strip()
        mapping = {
            "DEBUG": cls.DEBUG,
            "INFO": cls.INFO,
            "WARN": cls.WARNING,
            "WARNING": cls.WARNING,
            "ERROR": cls.ERROR,
            "ERR": cls.ERROR,
            "CRITICAL": cls.CRITICAL,
            "CRIT": cls.CRITICAL,
            "FATAL": cls.CRITICAL,
        }
        return mapping.get(s, cls.INFO)


@dataclass
class LogEntry:
    source: str
    level: LogLevel
    message: str
    logger_name: str = ""
    ts: float = field(default_factory=time.time)
    raw: str = ""
    fields: dict = field(default_factory=dict)  # parsed structured fields

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "level": self.level.value,
            "message": self.message[:200],
            "logger_name": self.logger_name,
            "ts": self.ts,
            "fields": self.fields,
        }


@dataclass
class LogSource:
    source_id: str
    kind: str  # "file" | "redis_stream" | "push"
    path: str = ""  # for file sources
    stream_key: str = ""  # for redis stream sources
    parse_json: bool = False
    tail_lines: int = 1000


@dataclass
class AggregationWindow:
    level: LogLevel
    count: int
    rate_per_min: float
    top_sources: list[tuple[str, int]]
    top_messages: list[tuple[str, int]]
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "count": self.count,
            "rate_per_min": round(self.rate_per_min, 2),
            "top_sources": self.top_sources[:5],
            "ts": self.ts,
        }


# Log line patterns
_PATTERNS = [
    # Python standard: "2026-01-01 12:00:00,123 INFO module: message"
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d+)\s+"
        r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+"
        r"(?P<logger>\S+):\s+(?P<msg>.+)$"
    ),
    # Systemd: "Jan 01 12:00:00 host service[pid]: message"
    re.compile(r"^(?P<ts>\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+\S+\s+\S+:\s+(?P<msg>.+)$"),
    # Generic "LEVEL: message"
    re.compile(
        r"^(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)[:\s]+(?P<msg>.+)$", re.I
    ),
]


def _parse_line(line: str, source: str) -> LogEntry:
    line = line.strip()
    if not line:
        return LogEntry(source=source, level=LogLevel.INFO, message="", raw=line)

    # Try JSON first
    if line.startswith("{"):
        try:
            d = json.loads(line)
            level_str = d.get("level", d.get("severity", "info"))
            return LogEntry(
                source=source,
                level=LogLevel.from_str(str(level_str)),
                message=d.get("message", d.get("msg", line[:200])),
                logger_name=d.get("logger", d.get("name", "")),
                fields={
                    k: v
                    for k, v in d.items()
                    if k not in ("level", "message", "msg", "logger")
                },
                raw=line,
            )
        except json.JSONDecodeError:
            pass

    # Try regex patterns
    for pattern in _PATTERNS:
        m = pattern.match(line)
        if m:
            gd = m.groupdict()
            return LogEntry(
                source=source,
                level=LogLevel.from_str(gd.get("level", "info")),
                message=gd.get("msg", line[:200]),
                logger_name=gd.get("logger", ""),
                raw=line,
            )

    return LogEntry(source=source, level=LogLevel.INFO, message=line[:200], raw=line)


class LogAggregator:
    def __init__(self, max_buffer: int = 5000):
        self.redis: aioredis.Redis | None = None
        self._sources: dict[str, LogSource] = {}
        self._buffer: list[LogEntry] = []
        self._max_buffer = max_buffer
        self._handlers: list[Callable] = []
        self._running = False
        self._stats: dict[str, int] = {
            "ingested": 0,
            "errors": 0,
            "sources": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_source(self, source: LogSource):
        self._sources[source.source_id] = source
        self._stats["sources"] += 1

    def on_entry(self, handler: Callable):
        """Register handler(LogEntry) for each ingested log line."""
        self._handlers.append(handler)

    def ingest(self, line: str, source_id: str = "push") -> LogEntry:
        entry = _parse_line(line, source_id)
        self._buffer.append(entry)
        if len(self._buffer) > self._max_buffer:
            self._buffer.pop(0)
        self._stats["ingested"] += 1
        for h in self._handlers:
            try:
                h(entry)
            except Exception:
                pass
        return entry

    def ingest_many(self, lines: list[str], source_id: str = "push") -> list[LogEntry]:
        return [self.ingest(line, source_id) for line in lines if line.strip()]

    async def _tail_file(self, source: LogSource):
        path = Path(source.path)
        if not path.exists():
            return
        try:
            text = path.read_text(errors="replace")
            lines = text.splitlines()[-source.tail_lines :]
            for line in lines:
                self.ingest(line, source.source_id)
        except Exception as e:
            self._stats["errors"] += 1
            log.warning(f"File tail error {source.path}: {e}")

    async def _poll_redis_stream(self, source: LogSource):
        if not self.redis:
            return
        try:
            entries = await self.redis.xrange(source.stream_key, count=100)
            for entry_id, fields in entries:
                msg = fields.get("message", fields.get("msg", str(fields)))
                self.ingest(msg, source.source_id)
        except Exception as e:
            self._stats["errors"] += 1
            log.warning(f"Redis stream poll error {source.stream_key}: {e}")

    async def collect_all(self):
        tasks = []
        for src in self._sources.values():
            if src.kind == "file":
                tasks.append(self._tail_file(src))
            elif src.kind == "redis_stream":
                tasks.append(self._poll_redis_stream(src))
        if tasks:
            await asyncio.gather(*tasks)

    async def run(self, poll_interval_s: float = 10.0):
        self._running = True
        while self._running:
            await self.collect_all()
            await asyncio.sleep(poll_interval_s)

    def stop(self):
        self._running = False

    def query(
        self,
        level: LogLevel | None = None,
        source: str | None = None,
        pattern: str | None = None,
        since_ts: float = 0.0,
        limit: int = 100,
    ) -> list[LogEntry]:
        entries = list(reversed(self._buffer))
        if level:
            _order = [
                LogLevel.DEBUG,
                LogLevel.INFO,
                LogLevel.WARNING,
                LogLevel.ERROR,
                LogLevel.CRITICAL,
            ]
            min_idx = _order.index(level)
            entries = [e for e in entries if _order.index(e.level) >= min_idx]
        if source:
            entries = [e for e in entries if source in e.source]
        if since_ts:
            entries = [e for e in entries if e.ts >= since_ts]
        if pattern:
            pat = re.compile(pattern, re.I)
            entries = [e for e in entries if pat.search(e.message)]
        return entries[:limit]

    def aggregate(self, window_s: float = 60.0) -> list[AggregationWindow]:
        since = time.time() - window_s
        recent = [e for e in self._buffer if e.ts >= since]
        results = []
        for level in LogLevel:
            leveled = [e for e in recent if e.level == level]
            if not leveled:
                continue
            source_counts: dict[str, int] = {}
            msg_counts: dict[str, int] = {}
            for e in leveled:
                source_counts[e.source] = source_counts.get(e.source, 0) + 1
                key = e.message[:60]
                msg_counts[key] = msg_counts.get(key, 0) + 1
            results.append(
                AggregationWindow(
                    level=level,
                    count=len(leveled),
                    rate_per_min=len(leveled) / (window_s / 60),
                    top_sources=sorted(source_counts.items(), key=lambda x: -x[1])[:5],
                    top_messages=sorted(msg_counts.items(), key=lambda x: -x[1])[:5],
                )
            )
        return results

    def tail(self, n: int = 20) -> list[dict]:
        return [e.to_dict() for e in self._buffer[-n:]]

    def stats(self) -> dict:
        return {
            **self._stats,
            "buffer_size": len(self._buffer),
            "error_count": sum(
                1
                for e in self._buffer
                if e.level in (LogLevel.ERROR, LogLevel.CRITICAL)
            ),
        }


def build_jarvis_log_aggregator() -> LogAggregator:
    agg = LogAggregator(max_buffer=5000)

    def alert_on_critical(entry: LogEntry):
        if entry.level == LogLevel.CRITICAL:
            log.critical(
                f"[AGGREGATOR] CRITICAL from {entry.source}: {entry.message[:100]}"
            )

    agg.on_entry(alert_on_critical)

    # Add standard JARVIS log sources
    agg.add_source(LogSource("jarvis_main", "file", path="/var/log/jarvis/jarvis.log"))
    agg.add_source(LogSource("syslog", "file", path="/var/log/syslog", tail_lines=200))
    return agg


async def main():
    import sys

    agg = build_jarvis_log_aggregator()
    await agg.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        sample_lines = [
            "2026-04-14 10:00:00,000 INFO jarvis.core: System started",
            "2026-04-14 10:00:01,123 WARNING jarvis.gpu: GPU0 temperature 78°C",
            "2026-04-14 10:00:02,456 ERROR jarvis.model: Connection refused to M2",
            '{"level":"info","message":"Request routed to qwen3.5","model":"qwen3.5-9b"}',
            '{"level":"error","message":"VRAM allocation failed","node":"m1"}',
            "DEBUG: health check passed for ol1",
            "CRITICAL: Redis connection lost — failover initiated",
        ]
        print(f"Ingesting {len(sample_lines)} log lines...")
        for line in sample_lines:
            agg.ingest(line, "demo")

        print("\nTail (last 5):")
        for e in agg.tail(5):
            icon = {
                "info": "ℹ️",
                "warning": "⚠️",
                "error": "❌",
                "critical": "🔥",
                "debug": "·",
            }.get(e["level"], "·")
            print(f"  {icon} [{e['level']:<10}] {e['source']:<12} {e['message'][:60]}")

        print("\nAggregation (60s window):")
        for w in agg.aggregate(window_s=3600):
            print(
                f"  {w.level.value:<10} count={w.count} rate={w.rate_per_min:.1f}/min"
            )

        errors = agg.query(level=LogLevel.ERROR)
        print(f"\nErrors: {len(errors)}")
        for e in errors:
            print(f"  {e.message[:70]}")

        print(f"\nStats: {json.dumps(agg.stats(), indent=2)}")

    elif cmd == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        await agg.collect_all()
        for e in agg.tail(n):
            print(json.dumps(e))

    elif cmd == "stats":
        print(json.dumps(agg.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
