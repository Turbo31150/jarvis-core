#!/usr/bin/env python3
"""
jarvis_data_transformer — ETL-style data transformation pipeline
Chain of transform functions (map/filter/enrich/validate) over structured data
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.data_transformer")

REDIS_PREFIX = "jarvis:transform:"


class TransformOp(str, Enum):
    MAP = "map"
    FILTER = "filter"
    FLAT_MAP = "flat_map"
    ENRICH = "enrich"
    VALIDATE = "validate"
    RENAME = "rename"
    DROP = "drop"
    CAST = "cast"
    AGGREGATE = "aggregate"


@dataclass
class TransformStep:
    name: str
    op: TransformOp
    fn: Callable
    description: str = ""
    skip_errors: bool = False  # if True, failed items are dropped instead of raising


@dataclass
class TransformResult:
    records_in: int
    records_out: int
    records_dropped: int
    records_errored: int
    duration_ms: float
    steps_applied: list[str]
    sample: list[dict]  # first few output records

    def to_dict(self) -> dict:
        return {
            "records_in": self.records_in,
            "records_out": self.records_out,
            "records_dropped": self.records_dropped,
            "records_errored": self.records_errored,
            "duration_ms": round(self.duration_ms, 1),
            "steps": self.steps_applied,
            "sample_count": len(self.sample),
        }


class DataTransformer:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._steps: list[TransformStep] = []
        self._stats: dict[str, int] = {
            "runs": 0,
            "records_processed": 0,
            "records_dropped": 0,
            "errors": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def add_step(self, step: TransformStep) -> "DataTransformer":
        self._steps.append(step)
        return self

    def map(
        self, name: str, fn: Callable, skip_errors: bool = False
    ) -> "DataTransformer":
        return self.add_step(
            TransformStep(name, TransformOp.MAP, fn, skip_errors=skip_errors)
        )

    def filter(self, name: str, fn: Callable) -> "DataTransformer":
        return self.add_step(TransformStep(name, TransformOp.FILTER, fn))

    def flat_map(self, name: str, fn: Callable) -> "DataTransformer":
        return self.add_step(TransformStep(name, TransformOp.FLAT_MAP, fn))

    def enrich(self, name: str, fn: Callable) -> "DataTransformer":
        """fn(record) → dict of fields to merge into record."""
        return self.add_step(TransformStep(name, TransformOp.ENRICH, fn))

    def rename(self, name: str, mapping: dict[str, str]) -> "DataTransformer":
        return self.add_step(
            TransformStep(
                name,
                TransformOp.RENAME,
                lambda r: {mapping.get(k, k): v for k, v in r.items()},
            )
        )

    def drop_fields(self, name: str, fields: list[str]) -> "DataTransformer":
        return self.add_step(
            TransformStep(
                name,
                TransformOp.DROP,
                lambda r: {k: v for k, v in r.items() if k not in fields},
            )
        )

    def cast(self, name: str, field_types: dict[str, type]) -> "DataTransformer":
        def _cast(record):
            result = dict(record)
            for field_name, t in field_types.items():
                if field_name in result:
                    try:
                        result[field_name] = t(result[field_name])
                    except Exception:
                        pass
            return result

        return self.add_step(TransformStep(name, TransformOp.CAST, _cast))

    def validate_fields(self, name: str, required: list[str]) -> "DataTransformer":
        def _validate(record):
            missing = [f for f in required if f not in record or record[f] is None]
            if missing:
                raise ValueError(f"Missing required fields: {missing}")
            return True

        return self.add_step(TransformStep(name, TransformOp.VALIDATE, _validate))

    def _apply_step(
        self, step: TransformStep, records: list[Any]
    ) -> tuple[list[Any], int, int]:
        out = []
        dropped = 0
        errors = 0

        for record in records:
            try:
                if step.op == TransformOp.MAP:
                    out.append(step.fn(record))
                elif step.op == TransformOp.FILTER:
                    if step.fn(record):
                        out.append(record)
                    else:
                        dropped += 1
                elif step.op == TransformOp.FLAT_MAP:
                    results = step.fn(record)
                    out.extend(results)
                elif step.op == TransformOp.ENRICH:
                    extra = step.fn(record)
                    merged = (
                        {**record, **(extra or {})}
                        if isinstance(record, dict)
                        else record
                    )
                    out.append(merged)
                elif step.op == TransformOp.RENAME:
                    out.append(step.fn(record))
                elif step.op == TransformOp.DROP:
                    out.append(step.fn(record))
                elif step.op == TransformOp.CAST:
                    out.append(step.fn(record))
                elif step.op == TransformOp.VALIDATE:
                    step.fn(record)  # raises if invalid
                    out.append(record)
                else:
                    out.append(record)
            except Exception:
                if step.skip_errors:
                    errors += 1
                    dropped += 1
                else:
                    raise

        return out, dropped, errors

    def run(self, records: list[Any]) -> TransformResult:
        t0 = time.time()
        self._stats["runs"] += 1
        current = list(records)
        total_dropped = 0
        total_errors = 0
        applied = []

        for step in self._steps:
            current, dropped, errors = self._apply_step(step, current)
            total_dropped += dropped
            total_errors += errors
            applied.append(step.name)
            self._stats["records_dropped"] += dropped
            self._stats["errors"] += errors

        self._stats["records_processed"] += len(records)

        return TransformResult(
            records_in=len(records),
            records_out=len(current),
            records_dropped=total_dropped,
            records_errored=total_errors,
            duration_ms=(time.time() - t0) * 1000,
            steps_applied=applied,
            sample=current[:3] if current else [],
        )

    async def run_async(self, records: list[Any]) -> TransformResult:
        """Run in thread pool to avoid blocking."""
        return await asyncio.to_thread(self.run, records)

    def clear_steps(self):
        self._steps.clear()

    def stats(self) -> dict:
        return {**self._stats, "steps": len(self._steps)}


def build_jarvis_transformer() -> DataTransformer:
    transformer = DataTransformer()

    # Example: normalize LLM log records
    transformer.validate_fields("require_fields", ["ts", "model", "latency_ms"]).cast(
        "type_cast", {"latency_ms": float, "ts": float, "tokens": int}
    ).enrich(
        "add_tier",
        lambda r: {"tier": "fast" if r.get("latency_ms", 0) < 500 else "slow"},
    ).filter("drop_errors", lambda r: r.get("status") != "error").drop_fields(
        "clean", ["_raw", "_internal"]
    ).rename("normalize", {"latency_ms": "latency", "ts": "timestamp"})

    return transformer


async def main():
    import sys
    import random

    transformer = build_jarvis_transformer()
    await transformer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        random.seed(42)
        records = []
        for i in range(20):
            records.append(
                {
                    "ts": time.time() - random.randint(0, 3600),
                    "model": random.choice(["qwen3.5-9b", "qwen3.5-27b"]),
                    "latency_ms": random.gauss(500, 200),
                    "tokens": random.randint(50, 2000),
                    "status": "ok" if random.random() > 0.1 else "error",
                }
            )

        result = transformer.run(records)
        print(
            f"Records: {result.records_in} → {result.records_out} (dropped={result.records_dropped})"
        )
        print(f"Duration: {result.duration_ms:.1f}ms")
        print(f"Steps: {result.steps_applied}")
        if result.sample:
            print(
                f"\nSample record: {json.dumps(result.sample[0], indent=2, default=str)}"
            )
        print(f"\nStats: {json.dumps(transformer.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(transformer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
