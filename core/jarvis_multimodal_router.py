#!/usr/bin/env python3
"""
jarvis_multimodal_router — Route requests based on input modality
Detects text/image/audio/code content and routes to appropriate model/pipeline
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.multimodal_router")

REDIS_PREFIX = "jarvis:mm:"

NODES = {
    "M1": "http://127.0.0.1:1234",
    "M2": "http://192.168.1.26:1234",
    "OL1": "http://127.0.0.1:11434",
}


class Modality(str, Enum):
    TEXT = "text"
    CODE = "code"
    IMAGE = "image"
    AUDIO = "audio"
    MIXED = "mixed"
    EMBEDDING = "embedding"


@dataclass
class ModalityDetection:
    modality: Modality
    confidence: float
    hints: list[str] = field(default_factory=list)


@dataclass
class RouteDecision:
    modality: Modality
    node: str
    model: str
    endpoint: str
    reason: str
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "modality": self.modality.value,
            "node": self.node,
            "model": self.model,
            "endpoint": self.endpoint,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
        }


# Model capabilities per modality
MODALITY_MODELS: dict[Modality, list[dict]] = {
    Modality.TEXT: [
        {"node": "M1", "model": "qwen/qwen3.5-9b", "priority": 1},
        {"node": "M2", "model": "qwen/qwen3.5-9b", "priority": 2},
    ],
    Modality.CODE: [
        {"node": "M1", "model": "qwen/qwen3.5-27b-claude", "priority": 1},
        {"node": "M2", "model": "qwen/qwen3.5-35b-a3b", "priority": 2},
    ],
    Modality.IMAGE: [
        {"node": "M1", "model": "gemma-4-26b-a4b", "priority": 1},
    ],
    Modality.AUDIO: [
        {"node": "M1", "model": "whisper-large-v3", "priority": 1},
    ],
    Modality.EMBEDDING: [
        {"node": "M2", "model": "nomic-embed-text", "priority": 1},
        {"node": "M1", "model": "nomic-embed-text", "priority": 2},
    ],
    Modality.MIXED: [
        {"node": "M1", "model": "qwen/qwen3.5-27b-claude", "priority": 1},
    ],
}

CODE_PATTERNS = [
    r"```[\w]*\n",
    r"def \w+\(",
    r"class \w+[:\(]",
    r"import \w+",
    r"#!\s*/usr/bin",
    r"async def ",
    r"SELECT .* FROM",
    r"\$\{.*\}",
]

IMAGE_INDICATORS = ["data:image/", "<img", ".png", ".jpg", ".jpeg", ".gif", ".webp"]
AUDIO_INDICATORS = ["data:audio/", ".mp3", ".wav", ".ogg", ".flac", ".m4a"]


class MultimodalRouter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._route_cache: dict[str, RouteDecision] = {}
        self._stats: dict[str, int] = {m.value: 0 for m in Modality}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def detect(self, content: Any) -> ModalityDetection:
        """Detect modality from content."""
        hints = []

        if isinstance(content, bytes) or (
            isinstance(content, str) and content.startswith("data:image/")
        ):
            return ModalityDetection(Modality.IMAGE, 0.99, ["binary_or_data_url"])

        if isinstance(content, str) and any(ind in content for ind in AUDIO_INDICATORS):
            return ModalityDetection(Modality.AUDIO, 0.95, ["audio_indicator"])

        if isinstance(content, list):
            # Multi-part message (OpenAI vision format)
            has_image = any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in content
            )
            if has_image:
                return ModalityDetection(Modality.IMAGE, 0.99, ["vision_message"])
            return ModalityDetection(Modality.MIXED, 0.8, ["multipart"])

        if not isinstance(content, str):
            content = str(content)

        # Embedding requests
        if len(content) < 20 or content.strip().lower().startswith("embed:"):
            hints.append("short_or_embed_prefix")
            # Don't return embedding yet — could be short text

        # Code detection
        code_score = sum(
            1 for p in CODE_PATTERNS if re.search(p, content, re.MULTILINE)
        )
        if code_score >= 2:
            hints.append(f"code_patterns:{code_score}")
            return ModalityDetection(
                Modality.CODE, min(0.6 + code_score * 0.1, 0.99), hints
            )

        # Image check
        if any(ind in content for ind in IMAGE_INDICATORS):
            hints.append("image_indicator")
            return ModalityDetection(Modality.IMAGE, 0.9, hints)

        hints.append("plain_text")
        return ModalityDetection(Modality.TEXT, 0.95, hints)

    async def route(
        self,
        content: Any,
        force_modality: Modality | None = None,
        prefer_node: str | None = None,
    ) -> RouteDecision:
        t0 = time.time()

        detection = (
            ModalityDetection(force_modality, 1.0, ["forced"])
            if force_modality
            else self.detect(content)
        )

        candidates = MODALITY_MODELS.get(
            detection.modality, MODALITY_MODELS[Modality.TEXT]
        )

        if prefer_node:
            preferred = [c for c in candidates if c["node"] == prefer_node]
            if preferred:
                candidates = preferred + [
                    c for c in candidates if c["node"] != prefer_node
                ]

        # Check node availability
        chosen = None
        for candidate in candidates:
            if await self._node_alive(candidate["node"]):
                chosen = candidate
                break

        if not chosen:
            chosen = candidates[0]  # fallback even if node might be down

        node_url = NODES.get(chosen["node"], NODES["M1"])
        endpoint = f"{node_url}/v1/chat/completions"
        if detection.modality == Modality.AUDIO:
            endpoint = f"{node_url}/v1/audio/transcriptions"
        elif detection.modality == Modality.EMBEDDING:
            endpoint = f"{node_url}/v1/embeddings"

        decision = RouteDecision(
            modality=detection.modality,
            node=chosen["node"],
            model=chosen["model"],
            endpoint=endpoint,
            reason=f"detected={detection.modality.value} conf={detection.confidence:.2f} hints={detection.hints}",
            latency_ms=round((time.time() - t0) * 1000, 1),
        )

        self._stats[detection.modality.value] += 1
        if self.redis:
            await self.redis.hincrby(
                f"{REDIS_PREFIX}stats", detection.modality.value, 1
            )

        log.debug(
            f"Route: {detection.modality.value} → {chosen['node']}:{chosen['model']}"
        )
        return decision

    async def _node_alive(self, node_id: str) -> bool:
        url = NODES.get(node_id, "")
        if not url:
            return False
        cache_key = f"alive:{node_id}"
        if cache_key in self._route_cache:
            return True  # treat cached as alive
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2)
            ) as sess:
                async with sess.get(f"{url}/v1/models") as r:
                    return r.status == 200
        except Exception:
            return False

    def stats(self) -> dict:
        return {
            "routes_by_modality": dict(self._stats),
            "total": sum(self._stats.values()),
        }


async def main():
    import sys

    router = MultimodalRouter()
    await router.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        test_inputs = [
            "What is the capital of France?",
            "def fibonacci(n):\n    if n <= 1: return n\n    return fibonacci(n-1) + fibonacci(n-2)",
            "```python\nimport asyncio\nasync def main(): pass\n```",
            "data:image/png;base64,iVBORw0KGgo=",
            [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        ]
        for inp in test_inputs:
            detection = router.detect(inp)
            decision = await router.route(inp)
            label = str(inp)[:40].replace("\n", "\\n")
            print(
                f"[{detection.modality.value:<10}] {label:<40} → {decision.node}:{decision.model.split('/')[-1]}"
            )

    elif cmd == "stats":
        print(json.dumps(router.stats(), indent=2))

    elif cmd == "detect" and len(sys.argv) > 2:
        text = " ".join(sys.argv[2:])
        d = router.detect(text)
        print(f"Modality: {d.modality.value} (confidence={d.confidence:.2f})")
        print(f"Hints: {d.hints}")


if __name__ == "__main__":
    asyncio.run(main())
