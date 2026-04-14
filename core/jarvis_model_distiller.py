#!/usr/bin/env python3
"""
jarvis_model_distiller — Knowledge distillation pipeline tracker
Generates teacher→student training pairs, tracks quality metrics, manages datasets
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.model_distiller")

DISTILL_DIR = Path("/home/turbo/IA/Core/jarvis/data/distillation")
REDIS_PREFIX = "jarvis:distill:"

TEACHER_MODEL = "qwen/qwen3.5-27b-claude-4.6-opus-distilled"
STUDENT_MODEL = "qwen/qwen3.5-9b"
LM_URL = "http://127.0.0.1:1234"


@dataclass
class DistillPair:
    pair_id: str
    prompt: str
    teacher_response: str
    student_response: str = ""
    teacher_model: str = TEACHER_MODEL
    student_model: str = STUDENT_MODEL
    quality_score: float = 0.0  # 0-1, teacher response quality
    divergence: float = 0.0  # how different student is from teacher
    ts: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pair_id": self.pair_id,
            "prompt": self.prompt[:500],
            "teacher_response": self.teacher_response[:1000],
            "student_response": self.student_response[:1000],
            "teacher_model": self.teacher_model,
            "student_model": self.student_model,
            "quality_score": self.quality_score,
            "divergence": self.divergence,
            "ts": self.ts,
            "tags": self.tags,
        }

    def to_training_jsonl(self) -> str:
        """Format for fine-tuning (teacher as ground truth)."""
        return json.dumps(
            {
                "messages": [
                    {"role": "user", "content": self.prompt},
                    {"role": "assistant", "content": self.teacher_response},
                ]
            }
        )


@dataclass
class DistillDataset:
    name: str
    pairs: list[DistillPair] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def avg_quality(self) -> float:
        if not self.pairs:
            return 0.0
        return round(mean(p.quality_score for p in self.pairs), 3)

    @property
    def avg_divergence(self) -> float:
        if not self.pairs:
            return 0.0
        return round(mean(p.divergence for p in self.pairs if p.student_response), 3)

    def stats(self) -> dict:
        return {
            "name": self.name,
            "pairs": len(self.pairs),
            "avg_quality": self.avg_quality,
            "avg_divergence": self.avg_divergence,
            "high_quality": sum(1 for p in self.pairs if p.quality_score >= 0.8),
            "tags": list({t for p in self.pairs for t in p.tags}),
        }


class ModelDistiller:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        DISTILL_DIR.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    async def _infer(self, model: str, prompt: str, max_tokens: int = 512) -> str:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.7,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"Infer error ({model}): {e}")
        return ""

    def _estimate_quality(self, response: str, prompt: str) -> float:
        """Heuristic quality score 0-1."""
        if not response:
            return 0.0
        score = 0.5
        # Length signal
        if len(response) > 200:
            score += 0.1
        if len(response) > 500:
            score += 0.1
        # Structure signals
        if any(c in response for c in ["```", "1.", "- ", "##"]):
            score += 0.1
        # Relevance: prompt keywords in response
        prompt_words = set(prompt.lower().split())
        resp_words = set(response.lower().split())
        overlap = len(prompt_words & resp_words) / max(len(prompt_words), 1)
        score += min(0.2, overlap * 0.5)
        return min(1.0, round(score, 3))

    def _compute_divergence(self, teacher: str, student: str) -> float:
        """Word-level Jaccard divergence between responses."""
        if not teacher or not student:
            return 1.0
        t_words = set(teacher.lower().split())
        s_words = set(student.lower().split())
        if not t_words and not s_words:
            return 0.0
        intersection = len(t_words & s_words)
        union = len(t_words | s_words)
        return round(1.0 - intersection / max(union, 1), 3)

    async def generate_pair(
        self,
        prompt: str,
        tags: list[str] | None = None,
        include_student: bool = True,
    ) -> DistillPair:
        """Generate a teacher-student pair for one prompt."""
        log.info(f"Distilling: {prompt[:60]}...")

        teacher_resp, student_resp = await asyncio.gather(
            self._infer(TEACHER_MODEL, prompt),
            self._infer(STUDENT_MODEL, prompt) if include_student else asyncio.sleep(0),
        )
        if not include_student:
            student_resp = ""

        pair = DistillPair(
            pair_id=str(uuid.uuid4())[:8],
            prompt=prompt,
            teacher_response=teacher_resp,
            student_response=student_resp,
            quality_score=self._estimate_quality(teacher_resp, prompt),
            divergence=self._compute_divergence(teacher_resp, student_resp),
            tags=tags or [],
        )

        if self.redis:
            await self.redis.lpush(
                f"{REDIS_PREFIX}pairs",
                json.dumps(pair.to_dict()),
            )
            await self.redis.ltrim(f"{REDIS_PREFIX}pairs", 0, 9999)

        return pair

    async def build_dataset(
        self,
        prompts: list[str],
        name: str,
        tags: list[str] | None = None,
        include_student: bool = True,
        min_quality: float = 0.6,
    ) -> DistillDataset:
        """Build a full distillation dataset from a list of prompts."""
        dataset = DistillDataset(name=name)
        log.info(f"Building dataset '{name}' from {len(prompts)} prompts")

        for i, prompt in enumerate(prompts):
            pair = await self.generate_pair(
                prompt, tags=tags, include_student=include_student
            )
            if pair.quality_score >= min_quality:
                dataset.pairs.append(pair)
                log.debug(
                    f"[{i + 1}/{len(prompts)}] quality={pair.quality_score:.2f} divergence={pair.divergence:.2f}"
                )
            else:
                log.info(f"Skipped low-quality pair: quality={pair.quality_score:.2f}")

        self._save_dataset(dataset)
        return dataset

    def _save_dataset(self, dataset: DistillDataset):
        path = DISTILL_DIR / f"{dataset.name}.jsonl"
        with open(path, "w") as f:
            for pair in dataset.pairs:
                f.write(pair.to_training_jsonl() + "\n")
        meta_path = DISTILL_DIR / f"{dataset.name}_meta.json"
        meta_path.write_text(json.dumps(dataset.stats(), indent=2))
        log.info(f"Dataset saved: {path} ({len(dataset.pairs)} pairs)")

    def list_datasets(self) -> list[dict]:
        result = []
        for p in sorted(DISTILL_DIR.glob("*_meta.json")):
            try:
                result.append(json.loads(p.read_text()))
            except Exception:
                pass
        return result

    def load_dataset(self, name: str) -> list[dict]:
        path = DISTILL_DIR / f"{name}.jsonl"
        if not path.exists():
            return []
        pairs = []
        for line in path.read_text().strip().split("\n"):
            if line.strip():
                try:
                    pairs.append(json.loads(line))
                except Exception:
                    pass
        return pairs


async def main():
    import sys

    distiller = ModelDistiller()
    await distiller.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        prompts = [
            "Explain what a GPU kernel is in 2 sentences.",
            "What is the difference between asyncio and threading in Python?",
            "How does Redis pub/sub work?",
        ]
        dataset = await distiller.build_dataset(
            prompts, name="demo_distill", tags=["tech", "demo"], include_student=False
        )
        s = dataset.stats()
        print(
            f"Dataset '{s['name']}': {s['pairs']} pairs, avg_quality={s['avg_quality']:.2f}"
        )

    elif cmd == "list":
        for ds in distiller.list_datasets():
            print(
                f"  {ds['name']:<30} {ds['pairs']:>5} pairs  quality={ds['avg_quality']:.2f}"
            )

    elif cmd == "show" and len(sys.argv) > 2:
        pairs = distiller.load_dataset(sys.argv[2])
        print(f"{len(pairs)} training examples")
        for p in pairs[:3]:
            user_msg = p["messages"][0]["content"][:60]
            asst_msg = p["messages"][1]["content"][:80]
            print(f"  Q: {user_msg}")
            print(f"  A: {asst_msg}\n")


if __name__ == "__main__":
    asyncio.run(main())
