#!/usr/bin/env python3
"""
jarvis_pipeline_registry — Named pipeline definitions with versioning and execution
Register, version, and run named processing pipelines composed of steps
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.pipeline_registry")

REGISTRY_FILE = Path("/home/turbo/IA/Core/jarvis/data/pipeline_registry.json")
REDIS_PREFIX = "jarvis:pipreg:"


@dataclass
class PipelineStep:
    name: str
    handler: str  # reference to handler key
    config: dict = field(default_factory=dict)
    timeout_s: float = 30.0
    optional: bool = False  # if True, failure doesn't abort pipeline
    retry: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "handler": self.handler,
            "config": self.config,
            "timeout_s": self.timeout_s,
            "optional": self.optional,
            "retry": self.retry,
        }


@dataclass
class PipelineDefinition:
    pipeline_id: str
    name: str
    version: int
    steps: list[PipelineStep]
    description: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    active: bool = True

    def to_dict(self) -> dict:
        return {
            "pipeline_id": self.pipeline_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": self.tags,
            "steps": [s.to_dict() for s in self.steps],
            "active": self.active,
            "created_at": self.created_at,
        }


@dataclass
class PipelineRun:
    run_id: str
    pipeline_id: str
    pipeline_name: str
    version: int
    status: str = "running"  # running | done | failed | aborted
    input_data: Any = None
    output_data: Any = None
    step_results: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    error: str = ""

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.time()
        return round((end - self.started_at) * 1000, 1)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "pipeline_id": self.pipeline_id,
            "pipeline_name": self.pipeline_name,
            "version": self.version,
            "status": self.status,
            "step_count": len(self.step_results),
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class PipelineRegistry:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._pipelines: dict[str, list[PipelineDefinition]] = {}  # name → [versions]
        self._handlers: dict[str, Callable] = {}
        self._runs: list[PipelineRun] = []
        self._load()

    def _load(self):
        if REGISTRY_FILE.exists():
            try:
                data = json.loads(REGISTRY_FILE.read_text())
                for pid_data in data.get("pipelines", []):
                    steps = [PipelineStep(**s) for s in pid_data.pop("steps", [])]
                    defn = PipelineDefinition(
                        steps=steps,
                        **{
                            k: v
                            for k, v in pid_data.items()
                            if k in PipelineDefinition.__dataclass_fields__
                        },
                    )
                    name = defn.name
                    if name not in self._pipelines:
                        self._pipelines[name] = []
                    self._pipelines[name].append(defn)
                log.debug(
                    f"Loaded {sum(len(v) for v in self._pipelines.values())} pipeline versions"
                )
            except Exception as e:
                log.warning(f"Registry load error: {e}")

    def _save(self):
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        all_defns = [
            defn.to_dict() for versions in self._pipelines.values() for defn in versions
        ]
        REGISTRY_FILE.write_text(json.dumps({"pipelines": all_defns}, indent=2))

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_handler(self, handler_key: str, fn: Callable):
        self._handlers[handler_key] = fn

    def register(
        self,
        name: str,
        steps: list[PipelineStep],
        description: str = "",
        tags: list[str] | None = None,
        pipeline_id: str | None = None,
    ) -> PipelineDefinition:
        existing = self._pipelines.get(name, [])
        version = (max(d.version for d in existing) + 1) if existing else 1

        # Deactivate old versions
        for old in existing:
            old.active = False

        defn = PipelineDefinition(
            pipeline_id=pipeline_id or str(uuid.uuid4())[:8],
            name=name,
            version=version,
            steps=steps,
            description=description,
            tags=tags or [],
        )
        if name not in self._pipelines:
            self._pipelines[name] = []
        self._pipelines[name].append(defn)
        self._save()
        log.info(f"Pipeline registered: {name} v{version} ({len(steps)} steps)")
        return defn

    def get(self, name: str, version: int | None = None) -> PipelineDefinition | None:
        versions = self._pipelines.get(name, [])
        if not versions:
            return None
        if version is None:
            return next((d for d in reversed(versions) if d.active), versions[-1])
        return next((d for d in versions if d.version == version), None)

    async def run(
        self,
        name: str,
        input_data: Any = None,
        version: int | None = None,
        context: dict | None = None,
    ) -> PipelineRun:
        defn = self.get(name, version)
        if not defn:
            raise ValueError(f"Pipeline '{name}' not found")

        pipeline_run = PipelineRun(
            run_id=str(uuid.uuid4())[:8],
            pipeline_id=defn.pipeline_id,
            pipeline_name=name,
            version=defn.version,
            input_data=input_data,
        )
        self._runs.append(pipeline_run)

        ctx = context or {}
        data = input_data

        for step in defn.steps:
            handler = self._handlers.get(step.handler)
            if not handler:
                if step.optional:
                    pipeline_run.step_results.append(
                        {"step": step.name, "status": "skipped", "reason": "no handler"}
                    )
                    continue
                pipeline_run.status = "failed"
                pipeline_run.error = (
                    f"No handler '{step.handler}' for step '{step.name}'"
                )
                pipeline_run.ended_at = time.time()
                return pipeline_run

            t0 = time.time()
            for attempt in range(step.retry + 1):
                try:
                    if asyncio.iscoroutinefunction(handler):
                        result = await asyncio.wait_for(
                            handler(data, {**step.config, **ctx}),
                            timeout=step.timeout_s,
                        )
                    else:
                        result = handler(data, {**step.config, **ctx})
                    data = result
                    pipeline_run.step_results.append(
                        {
                            "step": step.name,
                            "status": "done",
                            "duration_ms": round((time.time() - t0) * 1000, 1),
                        }
                    )
                    break
                except Exception as e:
                    if attempt == step.retry:
                        if step.optional:
                            pipeline_run.step_results.append(
                                {
                                    "step": step.name,
                                    "status": "skipped",
                                    "error": str(e)[:100],
                                }
                            )
                        else:
                            pipeline_run.status = "failed"
                            pipeline_run.error = f"Step '{step.name}' failed: {e}"
                            pipeline_run.ended_at = time.time()
                            return pipeline_run

        pipeline_run.status = "done"
        pipeline_run.output_data = data
        pipeline_run.ended_at = time.time()

        if self.redis:
            await self.redis.lpush(
                f"{REDIS_PREFIX}runs", json.dumps(pipeline_run.to_dict())
            )
            await self.redis.ltrim(f"{REDIS_PREFIX}runs", 0, 999)

        log.info(
            f"Pipeline run [{pipeline_run.run_id}] {name} v{defn.version}: {pipeline_run.status} in {pipeline_run.duration_ms:.0f}ms"
        )
        return pipeline_run

    def list_pipelines(self) -> list[dict]:
        result = []
        for name, versions in self._pipelines.items():
            active = next((d for d in reversed(versions) if d.active), versions[-1])
            result.append(
                {
                    "name": name,
                    "version": active.version,
                    "steps": len(active.steps),
                    "description": active.description,
                    "tags": active.tags,
                }
            )
        return result

    def stats(self) -> dict:
        return {
            "pipelines": len(self._pipelines),
            "total_versions": sum(len(v) for v in self._pipelines.values()),
            "handlers": list(self._handlers.keys()),
            "runs": len(self._runs),
            "completed": sum(1 for r in self._runs if r.status == "done"),
            "failed": sum(1 for r in self._runs if r.status == "failed"),
        }


async def main():
    import sys

    registry = PipelineRegistry()
    await registry.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Register handlers
        async def preprocess(data, config):
            await asyncio.sleep(0.01)
            return {"text": str(data).strip().lower(), "config": config}

        async def embed(data, config):
            await asyncio.sleep(0.02)
            return {**data, "embedding": [0.1, 0.2, 0.3]}

        async def store(data, config):
            await asyncio.sleep(0.005)
            return {**data, "stored": True}

        registry.register_handler("preprocess", preprocess)
        registry.register_handler("embed", embed)
        registry.register_handler("store", store)

        # Register pipeline
        registry.register(
            "document_ingestion",
            steps=[
                PipelineStep("preprocess", "preprocess"),
                PipelineStep("embed", "embed", timeout_s=10.0),
                PipelineStep("store", "store"),
            ],
            description="Ingest and embed documents",
        )

        # Run it
        run = await registry.run("document_ingestion", "Hello World document")
        print(f"Run [{run.run_id}]: {run.status} in {run.duration_ms:.0f}ms")
        for step_result in run.step_results:
            print(
                f"  {step_result['step']}: {step_result['status']} ({step_result.get('duration_ms', 0):.0f}ms)"
            )

        print(f"\nStats: {json.dumps(registry.stats(), indent=2)}")

    elif cmd == "list":
        for p in registry.list_pipelines():
            print(
                f"  {p['name']:<30} v{p['version']} ({p['steps']} steps) {p['description']}"
            )

    elif cmd == "stats":
        print(json.dumps(registry.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
