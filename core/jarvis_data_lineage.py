#!/usr/bin/env python3
"""
jarvis_data_lineage — Data lineage tracking for JARVIS pipelines
Tracks data transformations, model inputs/outputs, and provenance chains
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.data_lineage")

LINEAGE_FILE = Path("/home/turbo/IA/Core/jarvis/data/lineage.jsonl")
REDIS_PREFIX = "jarvis:lineage:"


@dataclass
class LineageNode:
    node_id: str
    node_type: str  # source | transform | model | sink
    label: str
    data_hash: str  # SHA256 of the data at this point
    metadata: dict
    parents: list[str]  # parent node_ids
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "data_hash": self.data_hash,
            "metadata": self.metadata,
            "parents": self.parents,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LineageNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class LineageTrace:
    trace_id: str
    pipeline: str
    nodes: list[LineageNode] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    status: str = "active"

    @property
    def duration_s(self) -> float:
        return round((self.ended_at or time.time()) - self.started_at, 2)

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "pipeline": self.pipeline,
            "status": self.status,
            "duration_s": self.duration_s,
            "node_count": len(self.nodes),
            "nodes": [n.to_dict() for n in self.nodes],
        }


def _hash_data(data: Any) -> str:
    try:
        raw = json.dumps(data, sort_keys=True, default=str)
    except Exception:
        raw = str(data)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class DataLineage:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._traces: dict[str, LineageTrace] = {}
        LINEAGE_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def start_trace(self, pipeline: str) -> LineageTrace:
        trace = LineageTrace(
            trace_id=str(uuid.uuid4())[:8],
            pipeline=pipeline,
        )
        self._traces[trace.trace_id] = trace
        log.debug(f"Lineage trace started: [{trace.trace_id}] {pipeline}")
        return trace

    def record(
        self,
        trace_id: str,
        node_type: str,
        label: str,
        data: Any,
        metadata: dict | None = None,
        parents: list[str] | None = None,
    ) -> LineageNode:
        trace = self._traces.get(trace_id)
        if not trace:
            raise ValueError(f"Trace '{trace_id}' not found")

        # Auto-derive parents from last node if not specified
        if parents is None and trace.nodes:
            parents = [trace.nodes[-1].node_id]
        elif parents is None:
            parents = []

        node = LineageNode(
            node_id=str(uuid.uuid4())[:8],
            node_type=node_type,
            label=label,
            data_hash=_hash_data(data),
            metadata=metadata or {},
            parents=parents,
        )
        trace.nodes.append(node)
        return node

    def record_source(
        self, trace_id: str, label: str, data: Any, **meta
    ) -> LineageNode:
        return self.record(trace_id, "source", label, data, metadata=meta, parents=[])

    def record_transform(
        self, trace_id: str, label: str, data: Any, **meta
    ) -> LineageNode:
        return self.record(trace_id, "transform", label, data, metadata=meta)

    def record_model_call(
        self,
        trace_id: str,
        model: str,
        prompt: str,
        response: str,
        latency_ms: float = 0.0,
    ) -> LineageNode:
        return self.record(
            trace_id,
            "model",
            f"model:{model}",
            {"prompt_hash": _hash_data(prompt), "response_preview": response[:100]},
            metadata={
                "model": model,
                "latency_ms": latency_ms,
                "tokens_out": len(response.split()),
            },
        )

    def record_sink(self, trace_id: str, label: str, data: Any, **meta) -> LineageNode:
        return self.record(trace_id, "sink", label, data, metadata=meta)

    async def end_trace(self, trace_id: str, status: str = "completed"):
        trace = self._traces.get(trace_id)
        if not trace:
            return
        trace.status = status
        trace.ended_at = time.time()
        self._persist(trace)
        if self.redis:
            await self.redis.setex(
                f"{REDIS_PREFIX}trace:{trace_id}",
                86400,
                json.dumps(trace.to_dict()),
            )
            await self.redis.lpush(f"{REDIS_PREFIX}traces", trace_id)
            await self.redis.ltrim(f"{REDIS_PREFIX}traces", 0, 9999)
        log.debug(
            f"Lineage trace done: [{trace_id}] {status} ({len(trace.nodes)} nodes)"
        )

    def _persist(self, trace: LineageTrace):
        with open(LINEAGE_FILE, "a") as f:
            f.write(
                json.dumps(
                    {
                        "trace_id": trace.trace_id,
                        "pipeline": trace.pipeline,
                        "status": trace.status,
                        "duration_s": trace.duration_s,
                        "node_count": len(trace.nodes),
                        "ts": trace.started_at,
                    }
                )
                + "\n"
            )

    def get_trace(self, trace_id: str) -> LineageTrace | None:
        return self._traces.get(trace_id)

    def lineage_of(self, trace_id: str, node_id: str) -> list[LineageNode]:
        """Return full ancestor chain for a node (breadth-first)."""
        trace = self._traces.get(trace_id)
        if not trace:
            return []
        node_map = {n.node_id: n for n in trace.nodes}
        result = []
        queue = [node_id]
        visited = set()
        while queue:
            nid = queue.pop(0)
            if nid in visited:
                continue
            visited.add(nid)
            node = node_map.get(nid)
            if node:
                result.append(node)
                queue.extend(node.parents)
        return result

    def stats(self) -> dict:
        total = len(self._traces)
        completed = sum(1 for t in self._traces.values() if t.status == "completed")
        node_counts = [len(t.nodes) for t in self._traces.values()]
        return {
            "traces": total,
            "completed": completed,
            "active": sum(1 for t in self._traces.values() if t.status == "active"),
            "avg_nodes": round(sum(node_counts) / max(total, 1), 1),
        }


async def main():
    import sys

    lineage = DataLineage()
    await lineage.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        trace = lineage.start_trace("rag_pipeline")
        lineage.record_source(
            trace.trace_id, "user_query", "What is Redis?", user="turbo"
        )
        lineage.record_transform(
            trace.trace_id, "embed_query", [0.1, 0.2, 0.3], model="nomic-embed"
        )
        lineage.record_transform(
            trace.trace_id, "vector_search", ["doc1", "doc2"], top_k=5
        )
        lineage.record_transform(
            trace.trace_id, "context_assembly", "Redis is...", chunks=2
        )
        lineage.record_model_call(
            trace.trace_id,
            "qwen3.5-9b",
            "What is Redis?",
            "Redis is an in-memory store.",
            243.0,
        )
        lineage.record_sink(
            trace.trace_id, "user_response", "Redis is an in-memory store."
        )
        await lineage.end_trace(trace.trace_id)

        t = lineage.get_trace(trace.trace_id)
        print(
            f"Trace [{t.trace_id}] {t.pipeline}: {len(t.nodes)} nodes in {t.duration_s:.2f}s"
        )
        for node in t.nodes:
            parents = f"← {node.parents}" if node.parents else ""
            print(
                f"  [{node.node_type:<10}] {node.label:<30} hash={node.data_hash} {parents}"
            )

    elif cmd == "stats":
        print(json.dumps(lineage.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
