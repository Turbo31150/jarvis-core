#!/usr/bin/env python3
"""
jarvis_prompt_compressor — Lossless and lossy prompt compression
Reduces token count via deduplication, summarization, and selective pruning
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_compressor")

LM_URL = "http://127.0.0.1:1234"
COMPRESS_MODEL = "qwen/qwen3.5-9b"
REDIS_PREFIX = "jarvis:compress:"

CHARS_PER_TOKEN = 4


def _tok(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class CompressionResult:
    original: str
    compressed: str
    original_tokens: int
    compressed_tokens: int
    strategy: str
    ratio: float
    latency_ms: float = 0.0

    @property
    def savings_pct(self) -> float:
        return round((1 - self.ratio) * 100, 1)

    def to_dict(self) -> dict:
        return {
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "ratio": round(self.ratio, 3),
            "savings_pct": self.savings_pct,
            "strategy": self.strategy,
            "latency_ms": round(self.latency_ms, 1),
        }


class PromptCompressor:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._cache: dict[str, CompressionResult] = {}
        self._stats: dict[str, int] = {
            "lossless": 0,
            "lossy": 0,
            "llm": 0,
            "skipped": 0,
        }

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    # ── Lossless strategies ────────────────────────────────────────────────────

    def _remove_redundant_whitespace(self, text: str) -> str:
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"\t+", " ", text)
        return text.strip()

    def _remove_repeated_phrases(self, text: str) -> str:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        seen: dict[str, int] = {}
        result = []
        for sent in sentences:
            key = sent.strip().lower()
            count = seen.get(key, 0)
            if count < 2:
                result.append(sent)
                seen[key] = count + 1
        return " ".join(result)

    def _compress_code_blocks(self, text: str) -> str:
        """Keep code blocks but remove trailing blank lines within them."""

        def clean_block(m: re.Match) -> str:
            lang = m.group(1)
            code = re.sub(r"\n{2,}", "\n", m.group(2))
            return f"```{lang}\n{code.strip()}\n```"

        return re.sub(r"```(\w*)\n([\s\S]*?)```", clean_block, text)

    def lossless(self, text: str) -> CompressionResult:
        t0 = time.time()
        orig_tokens = _tok(text)
        compressed = self._remove_redundant_whitespace(text)
        compressed = self._remove_repeated_phrases(compressed)
        compressed = self._compress_code_blocks(compressed)
        comp_tokens = _tok(compressed)
        ratio = comp_tokens / max(orig_tokens, 1)
        self._stats["lossless"] += 1
        return CompressionResult(
            original=text,
            compressed=compressed,
            original_tokens=orig_tokens,
            compressed_tokens=comp_tokens,
            strategy="lossless",
            ratio=ratio,
            latency_ms=(time.time() - t0) * 1000,
        )

    # ── Lossy strategies ───────────────────────────────────────────────────────

    def _truncate_middle(self, text: str, target_tokens: int) -> str:
        """Keep beginning and end, cut the middle."""
        if _tok(text) <= target_tokens:
            return text
        chars = target_tokens * CHARS_PER_TOKEN
        half = chars // 2
        return text[:half] + "\n[... truncated ...]\n" + text[-half:]

    def _extract_key_sentences(self, text: str, keep_ratio: float = 0.5) -> str:
        """Keep sentences with high information density (heuristic)."""
        sentences = re.split(r"(?<=[.!?])\s+", text)
        if len(sentences) <= 3:
            return text

        def score(s: str) -> float:
            # Prefer sentences with numbers, proper nouns, keywords
            score = len(s.split()) * 0.1
            score += len(re.findall(r"\b[A-Z][a-z]+\b", s)) * 0.3
            score += len(re.findall(r"\d+", s)) * 0.2
            score += (
                len(
                    re.findall(
                        r"\b(important|key|critical|must|should|error|fail)\b", s, re.I
                    )
                )
                * 0.5
            )
            return score

        scored = sorted(enumerate(sentences), key=lambda x: score(x[1]), reverse=True)
        keep_n = max(3, int(len(sentences) * keep_ratio))
        kept_indices = sorted(i for i, _ in scored[:keep_n])
        return " ".join(sentences[i] for i in kept_indices)

    def lossy(
        self,
        text: str,
        target_ratio: float = 0.6,
        strategy: str = "extract",
    ) -> CompressionResult:
        t0 = time.time()
        orig_tokens = _tok(text)
        target_tokens = int(orig_tokens * target_ratio)

        if strategy == "truncate":
            compressed = self._truncate_middle(text, target_tokens)
        else:  # extract
            compressed = self._extract_key_sentences(text, keep_ratio=target_ratio)
            # If still too long, truncate
            if _tok(compressed) > target_tokens * 1.1:
                compressed = self._truncate_middle(compressed, target_tokens)

        comp_tokens = _tok(compressed)
        ratio = comp_tokens / max(orig_tokens, 1)
        self._stats["lossy"] += 1
        return CompressionResult(
            original=text,
            compressed=compressed,
            original_tokens=orig_tokens,
            compressed_tokens=comp_tokens,
            strategy=f"lossy:{strategy}",
            ratio=ratio,
            latency_ms=(time.time() - t0) * 1000,
        )

    # ── LLM-assisted compression ───────────────────────────────────────────────

    async def llm_compress(self, text: str, target_pct: int = 50) -> CompressionResult:
        t0 = time.time()
        orig_tokens = _tok(text)
        cache_key = text[:80]
        if cache_key in self._cache:
            return self._cache[cache_key]

        prompt = (
            f"Compress the following text to about {target_pct}% of its original length. "
            f"Preserve all key information, remove redundancy and filler words. "
            f"Return only the compressed text, nothing else.\n\n"
            f"Text:\n{text}\n\nCompressed:"
        )
        compressed = text  # fallback
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as sess:
                async with sess.post(
                    f"{LM_URL}/v1/chat/completions",
                    json={
                        "model": COMPRESS_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": orig_tokens,
                        "temperature": 0.1,
                    },
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        compressed = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.debug(f"LLM compress failed: {e}")

        comp_tokens = _tok(compressed)
        result = CompressionResult(
            original=text,
            compressed=compressed,
            original_tokens=orig_tokens,
            compressed_tokens=comp_tokens,
            strategy="llm",
            ratio=comp_tokens / max(orig_tokens, 1),
            latency_ms=(time.time() - t0) * 1000,
        )
        self._cache[cache_key] = result
        self._stats["llm"] += 1
        return result

    async def auto(
        self,
        text: str,
        target_ratio: float = 0.7,
        use_llm: bool = False,
    ) -> CompressionResult:
        """Auto-select best compression strategy."""
        orig_tokens = _tok(text)

        # Already small enough
        if orig_tokens < 200:
            self._stats["skipped"] += 1
            return CompressionResult(
                original=text,
                compressed=text,
                original_tokens=orig_tokens,
                compressed_tokens=orig_tokens,
                strategy="skipped",
                ratio=1.0,
            )

        # Try lossless first
        result = self.lossless(text)
        if result.ratio <= target_ratio:
            return result

        # Lossy
        result = self.lossy(text, target_ratio=target_ratio)
        if result.ratio <= target_ratio or not use_llm:
            return result

        # LLM as last resort
        return await self.llm_compress(text, target_pct=int(target_ratio * 100))

    def compress_messages(
        self,
        messages: list[dict],
        target_ratio: float = 0.7,
    ) -> list[dict]:
        """Compress a list of chat messages in-place (lossless only for safety)."""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 500:
                cr = self.lossless(content)
                result.append({**msg, "content": cr.compressed})
            else:
                result.append(msg)
        return result

    def stats(self) -> dict:
        return {
            "compressions": dict(self._stats),
            "total": sum(self._stats.values()),
            "cache_size": len(self._cache),
        }


async def main():
    import sys

    comp = PromptCompressor()
    await comp.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        sample = """
        Redis is an open-source, in-memory data structure store that can be used as a database,
        cache, and message broker. Redis supports data structures such as strings, hashes, lists,
        sets, sorted sets with range queries, bitmaps, hyperloglogs, geospatial indexes, and streams.
        Redis has built-in replication, Lua scripting, LRU eviction, transactions, and different
        levels of on-disk persistence. Redis also supports automatic partitioning through Redis Cluster.

        Redis is very fast. Redis is very fast. Redis is very fast because it stores data in memory.
        Redis also supports    lots    of   extra    whitespace   in   commands.

        The key features of Redis include:
        - Speed: Redis is extremely fast
        - Simplicity: Easy to use API
        - Persistence: Data can be saved to disk
        - Replication: Data can be replicated to slaves
        """
        result_ll = comp.lossless(sample)
        result_lo = comp.lossy(sample, target_ratio=0.5)

        print(f"Original:  {result_ll.original_tokens} tokens")
        print(
            f"Lossless:  {result_ll.compressed_tokens} tokens ({result_ll.savings_pct}% saved)"
        )
        print(
            f"Lossy:     {result_lo.compressed_tokens} tokens ({result_lo.savings_pct}% saved)"
        )
        print(f"\nLossless result:\n{result_ll.compressed[:300]}")

    elif cmd == "stats":
        print(json.dumps(comp.stats(), indent=2))

    elif cmd == "compress" and len(sys.argv) > 2:
        text = " ".join(sys.argv[2:])
        result = await comp.auto(text, use_llm=False)
        print(result.compressed)
        print(
            f"\n[{result.strategy}] {result.original_tokens}→{result.compressed_tokens} tokens ({result.savings_pct}% saved)"
        )


if __name__ == "__main__":
    asyncio.run(main())
