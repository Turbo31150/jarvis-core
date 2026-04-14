#!/usr/bin/env python3
"""
jarvis_prompt_versioning — Git-like versioning for prompts
Track prompt changes, rollback, diff, and A/B test across versions
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_versioning")

PROMPT_DB = Path("/home/turbo/IA/Core/jarvis/data/prompts.json")
REDIS_PREFIX = "jarvis:prompts:"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


@dataclass
class PromptVersion:
    version_id: str
    prompt_id: str
    content: str
    content_hash: str
    version: int
    author: str = "system"
    message: str = ""
    tags: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    performance: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "prompt_id": self.prompt_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "version": self.version,
            "author": self.author,
            "message": self.message,
            "tags": self.tags,
            "ts": self.ts,
            "performance": self.performance,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PromptVersion":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PromptRecord:
    prompt_id: str
    name: str
    description: str = ""
    versions: list[PromptVersion] = field(default_factory=list)
    active_version: int = 1
    created_at: float = field(default_factory=time.time)

    @property
    def latest(self) -> PromptVersion | None:
        return self.versions[-1] if self.versions else None

    @property
    def active(self) -> PromptVersion | None:
        for v in self.versions:
            if v.version == self.active_version:
                return v
        return self.latest

    def to_dict(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "name": self.name,
            "description": self.description,
            "active_version": self.active_version,
            "version_count": len(self.versions),
            "created_at": self.created_at,
        }


class PromptVersioning:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._prompts: dict[str, PromptRecord] = {}
        self._load()

    def _load(self):
        if PROMPT_DB.exists():
            try:
                data = json.loads(PROMPT_DB.read_text())
                for rec_data in data.get("prompts", []):
                    versions = [
                        PromptVersion.from_dict(v) for v in rec_data.pop("versions", [])
                    ]
                    rec = PromptRecord(
                        **{
                            k: v
                            for k, v in rec_data.items()
                            if k in PromptRecord.__dataclass_fields__
                        }
                    )
                    rec.versions = versions
                    self._prompts[rec.prompt_id] = rec
                log.debug(f"Loaded {len(self._prompts)} prompts")
            except Exception as e:
                log.warning(f"Prompt DB load error: {e}")

    def _save(self):
        PROMPT_DB.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for rec in self._prompts.values():
            d = rec.to_dict()
            d["versions"] = [v.to_dict() for v in rec.versions]
            data.append(d)
        PROMPT_DB.write_text(json.dumps({"prompts": data}, indent=2))

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def create(
        self,
        prompt_id: str,
        name: str,
        content: str,
        description: str = "",
        author: str = "system",
        message: str = "Initial version",
    ) -> PromptVersion:
        if prompt_id in self._prompts:
            return self.commit(prompt_id, content, author=author, message=message)

        ver = PromptVersion(
            version_id=f"{prompt_id}:v1",
            prompt_id=prompt_id,
            content=content,
            content_hash=_hash(content),
            version=1,
            author=author,
            message=message,
        )
        rec = PromptRecord(
            prompt_id=prompt_id,
            name=name,
            description=description,
            versions=[ver],
            active_version=1,
        )
        self._prompts[prompt_id] = rec
        self._save()
        log.info(f"Prompt created: {prompt_id} v1")
        return ver

    def commit(
        self,
        prompt_id: str,
        content: str,
        author: str = "system",
        message: str = "",
        tags: list[str] | None = None,
    ) -> PromptVersion:
        rec = self._prompts.get(prompt_id)
        if not rec:
            raise ValueError(f"Prompt '{prompt_id}' not found")

        latest = rec.latest
        if latest and latest.content == content:
            log.debug(f"No change in prompt {prompt_id}, skipping commit")
            return latest

        next_ver = (latest.version + 1) if latest else 1
        ver = PromptVersion(
            version_id=f"{prompt_id}:v{next_ver}",
            prompt_id=prompt_id,
            content=content,
            content_hash=_hash(content),
            version=next_ver,
            author=author,
            message=message or f"Version {next_ver}",
            tags=tags or [],
        )
        rec.versions.append(ver)
        self._save()
        log.info(f"Prompt committed: {prompt_id} v{next_ver}")
        return ver

    def get(self, prompt_id: str, version: int | None = None) -> PromptVersion | None:
        rec = self._prompts.get(prompt_id)
        if not rec:
            return None
        if version is None:
            return rec.active
        for v in rec.versions:
            if v.version == version:
                return v
        return None

    def get_content(self, prompt_id: str, version: int | None = None) -> str:
        ver = self.get(prompt_id, version)
        return ver.content if ver else ""

    def rollback(self, prompt_id: str, to_version: int) -> bool:
        rec = self._prompts.get(prompt_id)
        if not rec:
            return False
        for v in rec.versions:
            if v.version == to_version:
                rec.active_version = to_version
                self._save()
                log.info(f"Prompt {prompt_id} rolled back to v{to_version}")
                return True
        return False

    def set_active(self, prompt_id: str, version: int) -> bool:
        return self.rollback(prompt_id, version)

    def diff(self, prompt_id: str, v1: int, v2: int) -> dict:
        a = self.get(prompt_id, v1)
        b = self.get(prompt_id, v2)
        if not a or not b:
            return {"error": "Version not found"}

        # Simple line-level diff
        lines_a = a.content.splitlines()
        lines_b = b.content.splitlines()
        added = [l for l in lines_b if l not in lines_a]
        removed = [l for l in lines_a if l not in lines_b]

        return {
            "prompt_id": prompt_id,
            "v1": v1,
            "v2": v2,
            "hash_a": a.content_hash,
            "hash_b": b.content_hash,
            "added_lines": len(added),
            "removed_lines": len(removed),
            "added": added[:10],
            "removed": removed[:10],
            "identical": a.content_hash == b.content_hash,
        }

    def record_performance(self, prompt_id: str, version: int, metrics: dict):
        ver = self.get(prompt_id, version)
        if ver:
            ver.performance.update(metrics)
            self._save()

    def history(self, prompt_id: str) -> list[dict]:
        rec = self._prompts.get(prompt_id)
        if not rec:
            return []
        return [
            {
                "version": v.version,
                "hash": v.content_hash,
                "author": v.author,
                "message": v.message,
                "ts": v.ts,
                "active": v.version == rec.active_version,
                "performance": v.performance,
            }
            for v in rec.versions
        ]

    def list_prompts(self) -> list[dict]:
        return [r.to_dict() for r in self._prompts.values()]

    def stats(self) -> dict:
        recs = list(self._prompts.values())
        return {
            "prompts": len(recs),
            "total_versions": sum(len(r.versions) for r in recs),
            "avg_versions": round(
                sum(len(r.versions) for r in recs) / max(len(recs), 1), 1
            ),
        }


async def main():
    import sys

    pv = PromptVersioning()
    await pv.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        pv.create(
            "rag_system",
            "RAG System Prompt",
            content="You are JARVIS. Answer questions based on the provided context.",
            description="Main RAG system prompt",
        )

        pv.commit(
            "rag_system",
            content="You are JARVIS, an advanced AI assistant. Use the provided context to give accurate, helpful answers. If the context doesn't contain the answer, say so.",
            author="turbo",
            message="Improved instructions",
        )

        pv.commit(
            "rag_system",
            content="You are JARVIS, an advanced AI assistant. Use the provided context to give accurate, helpful answers. If the context doesn't contain the answer, say so clearly. Always cite your sources.",
            author="turbo",
            message="Added citation requirement",
        )

        pv.record_performance("rag_system", 2, {"relevance": 0.87, "accuracy": 0.91})
        pv.record_performance("rag_system", 3, {"relevance": 0.89, "accuracy": 0.93})

        for h in pv.history("rag_system"):
            active = " ← active" if h["active"] else ""
            perf = f" perf={h['performance']}" if h["performance"] else ""
            print(f"  v{h['version']} [{h['hash']}] {h['message']}{perf}{active}")

        diff = pv.diff("rag_system", 1, 3)
        print(
            f"\nDiff v1→v3: +{diff['added_lines']} lines, -{diff['removed_lines']} lines"
        )

    elif cmd == "list":
        for p in pv.list_prompts():
            print(
                f"  {p['prompt_id']:<25} {p['name']:<30} v{p['active_version']} ({p['version_count']} versions)"
            )

    elif cmd == "stats":
        print(json.dumps(pv.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
