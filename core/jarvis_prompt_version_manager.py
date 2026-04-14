#!/usr/bin/env python3
"""
jarvis_prompt_version_manager — Version control for LLM prompts with diff and rollback
Stores prompt versions, tracks changes, enables rollback, and A/B-friendly promotion
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_version_manager")

REDIS_PREFIX = "jarvis:promptver:"
VERSIONS_FILE = Path("/home/turbo/IA/Core/jarvis/data/prompt_versions.jsonl")


class PromptStage(str, Enum):
    DRAFT = "draft"
    STAGING = "staging"
    PRODUCTION = "production"
    ARCHIVED = "archived"


@dataclass
class PromptVersion:
    prompt_id: str
    version: int
    system: str
    user_template: str
    assistant_prefix: str = ""
    description: str = ""
    author: str = ""
    stage: PromptStage = PromptStage.DRAFT
    tags: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    promoted_at: float = 0.0

    @property
    def content_hash(self) -> str:
        content = f"{self.system}|{self.user_template}|{self.assistant_prefix}"
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def diff_from(self, other: "PromptVersion") -> dict[str, Any]:
        changes = {}
        for field_name in (
            "system",
            "user_template",
            "assistant_prefix",
            "description",
        ):
            a = getattr(other, field_name, "")
            b = getattr(self, field_name, "")
            if a != b:
                changes[field_name] = {"from": a[:200], "to": b[:200]}
        return changes

    def render(self, variables: dict[str, str] | None = None) -> dict[str, str]:
        import re

        vars_ = variables or {}

        def sub(text: str) -> str:
            def replacer(m: "re.Match") -> str:
                return str(vars_.get(m.group(1), m.group(0)))

            return re.sub(r"\{\{(\w+)\}\}", replacer, text)

        return {
            "system": sub(self.system),
            "user_template": sub(self.user_template),
            "assistant_prefix": sub(self.assistant_prefix),
        }

    def to_messages(self, variables: dict[str, str] | None = None) -> list[dict]:
        rendered = self.render(variables)
        messages = []
        if rendered["system"]:
            messages.append({"role": "system", "content": rendered["system"]})
        if rendered["user_template"]:
            messages.append({"role": "user", "content": rendered["user_template"]})
        if rendered["assistant_prefix"]:
            messages.append(
                {"role": "assistant", "content": rendered["assistant_prefix"]}
            )
        return messages

    def to_dict(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "system": self.system[:100],
            "user_template": self.user_template[:100],
            "stage": self.stage.value,
            "description": self.description,
            "author": self.author,
            "tags": self.tags,
            "metrics": self.metrics,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }


class PromptVersionManager:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._versions: dict[str, list[PromptVersion]] = {}  # prompt_id → [versions]
        self._production: dict[str, int] = {}  # prompt_id → version number
        self._stats: dict[str, int] = {
            "created": 0,
            "promoted": 0,
            "rolled_back": 0,
            "renders": 0,
        }
        VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _load(self):
        if not VERSIONS_FILE.exists():
            return
        try:
            for line in VERSIONS_FILE.read_text().splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                pv = PromptVersion(
                    prompt_id=d["prompt_id"],
                    version=d["version"],
                    system=d.get("system", ""),
                    user_template=d.get("user_template", ""),
                    assistant_prefix=d.get("assistant_prefix", ""),
                    description=d.get("description", ""),
                    author=d.get("author", ""),
                    stage=PromptStage(d.get("stage", "draft")),
                    tags=d.get("tags", []),
                    metrics=d.get("metrics", {}),
                    created_at=d.get("created_at", time.time()),
                )
                self._versions.setdefault(pv.prompt_id, []).append(pv)
                if pv.stage == PromptStage.PRODUCTION:
                    if pv.version > self._production.get(pv.prompt_id, -1):
                        self._production[pv.prompt_id] = pv.version
        except Exception as e:
            log.warning(f"Prompt version load error: {e}")

    def _save(self, pv: PromptVersion):
        with open(VERSIONS_FILE, "a") as f:
            f.write(
                json.dumps(
                    {
                        "prompt_id": pv.prompt_id,
                        "version": pv.version,
                        "system": pv.system,
                        "user_template": pv.user_template,
                        "assistant_prefix": pv.assistant_prefix,
                        "description": pv.description,
                        "author": pv.author,
                        "stage": pv.stage.value,
                        "tags": pv.tags,
                        "metrics": pv.metrics,
                        "created_at": pv.created_at,
                    }
                )
                + "\n"
            )

    def create(
        self,
        prompt_id: str,
        system: str,
        user_template: str = "",
        assistant_prefix: str = "",
        description: str = "",
        author: str = "",
        tags: list[str] | None = None,
    ) -> PromptVersion:
        versions = self._versions.get(prompt_id, [])
        next_ver = (max(v.version for v in versions) + 1) if versions else 1

        pv = PromptVersion(
            prompt_id=prompt_id,
            version=next_ver,
            system=system,
            user_template=user_template,
            assistant_prefix=assistant_prefix,
            description=description,
            author=author,
            tags=tags or [],
        )
        self._versions.setdefault(prompt_id, []).append(pv)
        self._save(pv)
        self._stats["created"] += 1
        log.info(f"Prompt {prompt_id} v{next_ver} created")
        return pv

    def promote(
        self, prompt_id: str, version: int, stage: PromptStage = PromptStage.PRODUCTION
    ) -> bool:
        pv = self._get_version(prompt_id, version)
        if not pv:
            return False
        pv.stage = stage
        pv.promoted_at = time.time()
        if stage == PromptStage.PRODUCTION:
            self._production[prompt_id] = version
        self._stats["promoted"] += 1
        log.info(f"Prompt {prompt_id} v{version} promoted to {stage.value}")
        return True

    def rollback(
        self, prompt_id: str, target_version: int | None = None
    ) -> PromptVersion | None:
        versions = self._versions.get(prompt_id, [])
        if not versions:
            return None

        if target_version:
            pv = self._get_version(prompt_id, target_version)
        else:
            # Roll back to previous production version
            prod_versions = sorted(
                [
                    v
                    for v in versions
                    if v.stage in (PromptStage.PRODUCTION, PromptStage.ARCHIVED)
                ],
                key=lambda v: v.version,
            )
            pv = (
                prod_versions[-2]
                if len(prod_versions) >= 2
                else prod_versions[0]
                if prod_versions
                else None
            )

        if pv:
            self._production[prompt_id] = pv.version
            pv.stage = PromptStage.PRODUCTION
            self._stats["rolled_back"] += 1
        return pv

    def _get_version(self, prompt_id: str, version: int) -> PromptVersion | None:
        return next(
            (v for v in self._versions.get(prompt_id, []) if v.version == version), None
        )

    def get_production(self, prompt_id: str) -> PromptVersion | None:
        ver = self._production.get(prompt_id)
        if ver is not None:
            return self._get_version(prompt_id, ver)
        versions = self._versions.get(prompt_id, [])
        return versions[-1] if versions else None

    def get_version(self, prompt_id: str, version: int) -> PromptVersion | None:
        return self._get_version(prompt_id, version)

    def render(
        self,
        prompt_id: str,
        variables: dict[str, str] | None = None,
        version: int | None = None,
    ) -> list[dict]:
        self._stats["renders"] += 1
        pv = (
            self._get_version(prompt_id, version)
            if version
            else self.get_production(prompt_id)
        )
        if not pv:
            return []
        return pv.to_messages(variables)

    def diff(self, prompt_id: str, v1: int, v2: int) -> dict[str, Any]:
        pv1 = self._get_version(prompt_id, v1)
        pv2 = self._get_version(prompt_id, v2)
        if not pv1 or not pv2:
            return {}
        return pv2.diff_from(pv1)

    def update_metrics(self, prompt_id: str, version: int, metrics: dict[str, float]):
        pv = self._get_version(prompt_id, version)
        if pv:
            pv.metrics.update(metrics)

    def list_versions(self, prompt_id: str) -> list[dict]:
        return [v.to_dict() for v in self._versions.get(prompt_id, [])]

    def list_prompts(self) -> list[str]:
        return list(self._versions.keys())

    def stats(self) -> dict:
        total_versions = sum(len(v) for v in self._versions.values())
        return {
            **self._stats,
            "prompts": len(self._versions),
            "total_versions": total_versions,
        }


def build_jarvis_prompt_manager() -> PromptVersionManager:
    mgr = PromptVersionManager()

    # Create initial versions for key prompts
    if "jarvis_system" not in mgr.list_prompts():
        v1 = mgr.create(
            "jarvis_system",
            system="You are JARVIS, a multi-agent AI orchestration system. Be concise and technical.",
            description="Initial JARVIS system prompt",
            author="system",
            tags=["core"],
        )
        mgr.promote("jarvis_system", v1.version)

    if "code_assistant" not in mgr.list_prompts():
        v1 = mgr.create(
            "code_assistant",
            system="You are an expert Python engineer. Write clean, typed, and tested code.",
            user_template="{{task}}\n\nContext:\n{{context}}",
            description="Code generation assistant",
            author="system",
            tags=["code"],
        )
        mgr.promote("code_assistant", v1.version)

    return mgr


async def main():
    import sys

    mgr = build_jarvis_prompt_manager()
    await mgr.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Create a new version
        v2 = mgr.create(
            "jarvis_system",
            system="You are JARVIS v2, an advanced multi-agent AI. Be extremely concise.",
            description="Improved system prompt with better conciseness",
            author="turbo",
        )
        print(f"Created v{v2.version}: {v2.description}")

        # Diff v1 vs v2
        d = mgr.diff("jarvis_system", 1, 2)
        print(f"\nDiff v1→v2: {json.dumps(d, indent=2)[:300]}")

        # Promote v2
        mgr.promote("jarvis_system", v2.version)

        # Render with variables
        msgs = mgr.render(
            "code_assistant", {"task": "Write a hello world", "context": "Python 3.12"}
        )
        print("\nRendered code_assistant:")
        for m in msgs:
            print(f"  [{m['role']}] {m['content'][:80]}")

        print("\nVersions for jarvis_system:")
        for v in mgr.list_versions("jarvis_system"):
            stage_icon = {
                "production": "✅",
                "draft": "📝",
                "staging": "🔄",
                "archived": "📦",
            }.get(v["stage"], "?")
            print(
                f"  {stage_icon} v{v['version']} {v['stage']:<12} {v['description'][:50]}"
            )

        print(f"\nStats: {json.dumps(mgr.stats(), indent=2)}")

    elif cmd == "list":
        for pid in mgr.list_prompts():
            pv = mgr.get_production(pid)
            print(f"  {pid:<30} prod=v{pv.version if pv else '?'}")

    elif cmd == "stats":
        print(json.dumps(mgr.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
