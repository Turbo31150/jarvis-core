#!/usr/bin/env python3
"""
jarvis_prompt_splitter — Splits large documents into prompt-sized chunks for LLM processing
Supports semantic boundaries, overlap, and multi-strategy splitting
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.prompt_splitter")


class SplitStrategy(str, Enum):
    FIXED_CHARS = "fixed_chars"  # hard character limit
    FIXED_TOKENS = "fixed_tokens"  # approximate token limit
    SENTENCES = "sentences"  # split on sentence boundaries
    PARAGRAPHS = "paragraphs"  # split on double newlines
    MARKDOWN_HEADERS = "markdown_headers"  # split on # headings
    RECURSIVE = "recursive"  # try paragraphs → sentences → chars
    CODE_BLOCKS = "code_blocks"  # split preserving code fences


@dataclass
class SplitConfig:
    strategy: SplitStrategy = SplitStrategy.RECURSIVE
    max_chunk_size: int = 2000  # chars (or ~tokens if FIXED_TOKENS)
    overlap: int = 200  # chars of overlap between chunks
    min_chunk_size: int = 100  # discard chunks smaller than this
    preserve_headers: bool = True  # keep nearest header as context prefix


@dataclass
class TextChunk:
    index: int
    text: str
    char_start: int
    char_end: int
    strategy_used: str = ""
    header_context: str = ""  # nearest markdown header if any
    approx_tokens: int = 0

    def __post_init__(self):
        if not self.approx_tokens:
            self.approx_tokens = max(1, len(self.text) // 4)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "text_preview": self.text[:80],
            "chars": len(self.text),
            "approx_tokens": self.approx_tokens,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "header_context": self.header_context,
            "strategy_used": self.strategy_used,
        }


# --- Splitters ---

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_SEP = re.compile(r"\n{2,}")
_MD_HEADER = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_CODE_FENCE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)


def _split_fixed_chars(text: str, max_size: int, overlap: int) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_size, len(text))
        chunks.append(text[start:end])
        start = end - overlap if end < len(text) else end
    return chunks


def _split_sentences(text: str, max_size: int, overlap: int) -> list[str]:
    sentences = _SENTENCE_END.split(text)
    chunks = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) + 1 <= max_size:
            current = (current + " " + sent).strip() if current else sent
        else:
            if current:
                chunks.append(current)
            if len(sent) > max_size:
                # Sentence itself too long — fall back to chars
                chunks.extend(_split_fixed_chars(sent, max_size, overlap))
                current = ""
            else:
                current = sent
    if current:
        chunks.append(current)
    return chunks


def _split_paragraphs(text: str, max_size: int, overlap: int) -> list[str]:
    paragraphs = [p.strip() for p in _PARAGRAPH_SEP.split(text) if p.strip()]
    chunks = []
    current = ""
    for para in paragraphs:
        sep = "\n\n" if current else ""
        if len(current) + len(sep) + len(para) <= max_size:
            current = current + sep + para
        else:
            if current:
                chunks.append(current)
            if len(para) > max_size:
                chunks.extend(_split_sentences(para, max_size, overlap))
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks


def _split_markdown_headers(text: str, max_size: int, overlap: int) -> list[str]:
    parts = _MD_HEADER.split(text)
    headers = _MD_HEADER.findall(text)
    chunks = []
    for i, part in enumerate(parts):
        header = headers[i - 1] if i > 0 and i - 1 < len(headers) else ""
        content = (header + "\n" + part).strip() if header else part.strip()
        if len(content) <= max_size:
            if content:
                chunks.append(content)
        else:
            chunks.extend(_split_paragraphs(content, max_size, overlap))
    return chunks if chunks else _split_paragraphs(text, max_size, overlap)


def _split_code_blocks(text: str, max_size: int, overlap: int) -> list[str]:
    """Split while keeping code fences intact."""
    parts = _CODE_FENCE.split(text)
    chunks = []
    current = ""
    for part in parts:
        if _CODE_FENCE.match(part):
            # Code block — keep as-is or split if huge
            if len(current) + len(part) <= max_size:
                current += part
            else:
                if current.strip():
                    chunks.append(current.strip())
                if len(part) > max_size:
                    chunks.extend(_split_fixed_chars(part, max_size, 0))
                else:
                    current = part
        else:
            if len(current) + len(part) <= max_size:
                current += part
            else:
                if current.strip():
                    chunks.append(current.strip())
                sub = _split_paragraphs(part, max_size, overlap)
                if sub:
                    chunks.extend(sub[:-1])
                    current = sub[-1] if sub else ""
                else:
                    current = ""
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _recursive_split(text: str, max_size: int, overlap: int) -> list[str]:
    if len(text) <= max_size:
        return [text] if text.strip() else []
    if "\n\n" in text:
        result = _split_paragraphs(text, max_size, overlap)
        if all(len(c) <= max_size for c in result):
            return result
    if ". " in text or "! " in text or "? " in text:
        result = _split_sentences(text, max_size, overlap)
        if all(len(c) <= max_size for c in result):
            return result
    return _split_fixed_chars(text, max_size, overlap)


def _extract_nearest_header(text: str, position: int, full_text: str) -> str:
    headers = list(_MD_HEADER.finditer(full_text))
    nearest = ""
    for m in headers:
        if m.start() <= position:
            nearest = m.group(0).strip()
        else:
            break
    return nearest


class PromptSplitter:
    def __init__(self, config: SplitConfig | None = None):
        self.redis: aioredis.Redis | None = None
        self._config = config or SplitConfig()
        self._stats: dict[str, int] = {"splits": 0, "total_chunks": 0, "total_chars": 0}

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def split(self, text: str, config: SplitConfig | None = None) -> list[TextChunk]:
        cfg = config or self._config
        self._stats["splits"] += 1
        self._stats["total_chars"] += len(text)

        strategy = cfg.strategy
        raw_chunks: list[str] = []

        if strategy == SplitStrategy.FIXED_CHARS:
            raw_chunks = _split_fixed_chars(text, cfg.max_chunk_size, cfg.overlap)
        elif strategy == SplitStrategy.FIXED_TOKENS:
            # approximate: 1 token ≈ 4 chars
            raw_chunks = _split_fixed_chars(
                text, cfg.max_chunk_size * 4, cfg.overlap * 4
            )
        elif strategy == SplitStrategy.SENTENCES:
            raw_chunks = _split_sentences(text, cfg.max_chunk_size, cfg.overlap)
        elif strategy == SplitStrategy.PARAGRAPHS:
            raw_chunks = _split_paragraphs(text, cfg.max_chunk_size, cfg.overlap)
        elif strategy == SplitStrategy.MARKDOWN_HEADERS:
            raw_chunks = _split_markdown_headers(text, cfg.max_chunk_size, cfg.overlap)
        elif strategy == SplitStrategy.CODE_BLOCKS:
            raw_chunks = _split_code_blocks(text, cfg.max_chunk_size, cfg.overlap)
        else:  # RECURSIVE
            raw_chunks = _recursive_split(text, cfg.max_chunk_size, cfg.overlap)

        # Filter min size and build TextChunk objects
        chunks = []
        offset = 0
        for i, chunk_text in enumerate(raw_chunks):
            if len(chunk_text.strip()) < cfg.min_chunk_size:
                continue
            char_start = text.find(chunk_text[:40], offset)
            if char_start == -1:
                char_start = offset
            char_end = char_start + len(chunk_text)
            offset = max(offset, char_start + 1)

            header = ""
            if cfg.preserve_headers:
                header = _extract_nearest_header(chunk_text, char_start, text)

            chunks.append(
                TextChunk(
                    index=len(chunks),
                    text=chunk_text.strip(),
                    char_start=char_start,
                    char_end=char_end,
                    strategy_used=strategy.value,
                    header_context=header,
                )
            )

        self._stats["total_chunks"] += len(chunks)
        return chunks

    def split_for_model(self, text: str, context_window: int = 4096) -> list[TextChunk]:
        """Split with config tuned for a given context window size."""
        max_chars = int(context_window * 3.5)  # ~4 chars/token with margin
        cfg = SplitConfig(
            strategy=SplitStrategy.RECURSIVE,
            max_chunk_size=max_chars,
            overlap=min(200, max_chars // 10),
        )
        return self.split(text, cfg)

    def stats(self) -> dict:
        avg_chunks = self._stats["total_chunks"] / max(self._stats["splits"], 1)
        return {**self._stats, "avg_chunks_per_split": round(avg_chunks, 1)}


def build_jarvis_prompt_splitter() -> PromptSplitter:
    return PromptSplitter(
        SplitConfig(strategy=SplitStrategy.RECURSIVE, max_chunk_size=2000, overlap=200)
    )


async def main():
    import sys

    splitter = build_jarvis_prompt_splitter()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        text = (
            """# JARVIS Cluster Architecture

## Overview

JARVIS is a multi-agent orchestration system running on a GPU cluster.
It handles LLM inference, embedding, monitoring, and trading operations.

## Nodes

The cluster consists of four nodes:

- **M1**: Primary inference node (192.168.1.85). Runs LM Studio with qwen3.5-9b and qwen3.5-27b.
- **M2**: Secondary inference node (192.168.1.26). Runs deepseek-r1-0528 and embedding models.
- **OL1**: Local Ollama node (127.0.0.1). Runs gemma3:4b for fast lightweight inference.
- **M3**: Offline node (192.168.1.133). Reserved for future expansion.

## Request Routing

Requests are routed based on capability requirements, current load, and latency history.
The inference gateway applies weighted round-robin with adaptive timeout management.

```python
router = build_jarvis_multimodel_router()
response = await router.call(messages, capabilities=[Capability.REASONING])
```

## Monitoring

GPU temperatures, VRAM utilization, and latency are tracked continuously.
Alerts fire when thresholds are exceeded.
"""
            * 3
        )  # Repeat to make it long enough to split

        print(f"Text length: {len(text)} chars")
        chunks = splitter.split(text)
        print(f"Chunks: {len(chunks)}\n")
        for c in chunks:
            print(
                f"  [{c.index}] chars={len(c.text):<5} tokens≈{c.approx_tokens:<5} "
                f"header='{c.header_context[:30]}' preview='{c.text[:50]}'"
            )

        print(f"\nStats: {json.dumps(splitter.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(splitter.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
