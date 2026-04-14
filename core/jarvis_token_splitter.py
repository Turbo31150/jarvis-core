#!/usr/bin/env python3
"""
jarvis_token_splitter — Text chunking and token-aware splitting for RAG pipelines
Splits documents respecting sentence boundaries, overlap, and token budgets
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger("jarvis.token_splitter")

# Approximate token/char ratio (GPT-style tokenisation)
_CHARS_PER_TOKEN = 4.0


class SplitStrategy(str, Enum):
    FIXED_TOKENS = "fixed_tokens"  # hard split at N tokens
    SENTENCE = "sentence"  # split at sentence boundaries
    PARAGRAPH = "paragraph"  # split at paragraph boundaries
    RECURSIVE = "recursive"  # try paragraph → sentence → fixed
    SEMANTIC = "semantic"  # placeholder: split at topic shifts


_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_PARA_RE = re.compile(r"\n{2,}")


def _approx_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _split_sentences(text: str) -> list[str]:
    parts = _SENT_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


def _split_paragraphs(text: str) -> list[str]:
    parts = _PARA_RE.split(text.strip())
    return [p.strip() for p in parts if p.strip()]


@dataclass
class TextChunk:
    chunk_id: str
    text: str
    token_count: int
    char_count: int
    chunk_index: int
    total_chunks: int = 0
    source: str = ""
    metadata: dict = field(default_factory=dict)
    start_char: int = 0
    end_char: int = 0

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "token_count": self.token_count,
            "char_count": self.char_count,
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "source": self.source,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "metadata": self.metadata,
        }


class TokenSplitter:
    def __init__(
        self,
        chunk_size: int = 512,  # target tokens per chunk
        chunk_overlap: int = 64,  # overlap tokens between chunks
        strategy: SplitStrategy = SplitStrategy.RECURSIVE,
        min_chunk_size: int = 32,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.strategy = strategy
        self.min_chunk_size = min_chunk_size
        self._stats: dict[str, int] = {"docs_split": 0, "chunks_produced": 0}

    def _make_chunk(
        self,
        text: str,
        index: int,
        source: str = "",
        start_char: int = 0,
        metadata: dict | None = None,
    ) -> TextChunk:
        tok = _approx_tokens(text)
        return TextChunk(
            chunk_id=f"{source}:{index}" if source else str(index),
            text=text,
            token_count=tok,
            char_count=len(text),
            chunk_index=index,
            source=source,
            start_char=start_char,
            end_char=start_char + len(text),
            metadata=metadata or {},
        )

    def _fixed_split(self, text: str, source: str = "") -> list[TextChunk]:
        """Hard split every chunk_size tokens with overlap."""
        step = max(1, self.chunk_size - self.chunk_overlap)
        step_chars = int(step * _CHARS_PER_TOKEN)
        size_chars = int(self.chunk_size * _CHARS_PER_TOKEN)
        chunks = []
        pos = 0
        idx = 0
        while pos < len(text):
            end = min(pos + size_chars, len(text))
            chunk_text = text[pos:end].strip()
            if _approx_tokens(chunk_text) >= self.min_chunk_size:
                chunks.append(self._make_chunk(chunk_text, idx, source, pos))
                idx += 1
            pos += step_chars
        return chunks

    def _sentence_split(self, text: str, source: str = "") -> list[TextChunk]:
        """Group sentences into chunks respecting token budget."""
        sentences = _split_sentences(text)
        return self._pack_units(sentences, text, source)

    def _paragraph_split(self, text: str, source: str = "") -> list[TextChunk]:
        paras = _split_paragraphs(text)
        return self._pack_units(paras, text, source)

    def _pack_units(
        self, units: list[str], original: str, source: str
    ) -> list[TextChunk]:
        chunks = []
        current_parts: list[str] = []
        current_tokens = 0
        idx = 0
        char_cursor = 0

        for unit in units:
            unit_tokens = _approx_tokens(unit)

            # Unit too large — sub-split it
            if unit_tokens > self.chunk_size:
                # Flush current
                if current_parts:
                    joined = " ".join(current_parts)
                    chunks.append(
                        self._make_chunk(joined, idx, source, char_cursor - len(joined))
                    )
                    idx += 1
                    current_parts = []
                    current_tokens = 0
                sub = self._fixed_split(unit, source)
                for s in sub:
                    s.chunk_index = idx
                    chunks.append(s)
                    idx += 1
                continue

            if current_tokens + unit_tokens > self.chunk_size and current_parts:
                joined = " ".join(current_parts)
                chunks.append(self._make_chunk(joined, idx, source))
                idx += 1
                # Keep overlap
                overlap_parts: list[str] = []
                overlap_tokens = 0
                for part in reversed(current_parts):
                    pt = _approx_tokens(part)
                    if overlap_tokens + pt <= self.chunk_overlap:
                        overlap_parts.insert(0, part)
                        overlap_tokens += pt
                    else:
                        break
                current_parts = overlap_parts
                current_tokens = overlap_tokens

            current_parts.append(unit)
            current_tokens += unit_tokens

        if current_parts:
            joined = " ".join(current_parts)
            if _approx_tokens(joined) >= self.min_chunk_size:
                chunks.append(self._make_chunk(joined, idx, source))

        return chunks

    def _recursive_split(self, text: str, source: str = "") -> list[TextChunk]:
        """Try paragraphs → sentences → fixed, picking the best fit."""
        paras = _split_paragraphs(text)
        if len(paras) > 1:
            return self._pack_units(paras, text, source)
        sents = _split_sentences(text)
        if len(sents) > 1:
            return self._pack_units(sents, text, source)
        return self._fixed_split(text, source)

    def split(
        self,
        text: str,
        source: str = "",
        metadata: dict | None = None,
    ) -> list[TextChunk]:
        text = text.strip()
        if not text:
            return []

        self._stats["docs_split"] += 1

        if self.strategy == SplitStrategy.FIXED_TOKENS:
            chunks = self._fixed_split(text, source)
        elif self.strategy == SplitStrategy.SENTENCE:
            chunks = self._sentence_split(text, source)
        elif self.strategy == SplitStrategy.PARAGRAPH:
            chunks = self._paragraph_split(text, source)
        else:
            chunks = self._recursive_split(text, source)

        # Assign total_chunks and metadata
        for c in chunks:
            c.total_chunks = len(chunks)
            if metadata:
                c.metadata.update(metadata)

        self._stats["chunks_produced"] += len(chunks)
        return chunks

    def split_file(self, path: Path, metadata: dict | None = None) -> list[TextChunk]:
        text = path.read_text(errors="replace")
        return self.split(text, source=str(path), metadata=metadata)

    def split_documents(
        self,
        docs: list[dict],  # [{text, source, metadata}]
    ) -> list[TextChunk]:
        all_chunks = []
        for doc in docs:
            chunks = self.split(
                doc.get("text", ""),
                source=doc.get("source", ""),
                metadata=doc.get("metadata"),
            )
            all_chunks.extend(chunks)
        return all_chunks

    def stats(self) -> dict:
        return {
            **self._stats,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "strategy": self.strategy.value,
        }


def build_jarvis_token_splitter(
    chunk_size: int = 512,
    overlap: int = 64,
    strategy: SplitStrategy = SplitStrategy.RECURSIVE,
) -> TokenSplitter:
    return TokenSplitter(
        chunk_size=chunk_size, chunk_overlap=overlap, strategy=strategy
    )


def main():
    import sys

    splitter = build_jarvis_token_splitter()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        sample = (
            """
        JARVIS is a multi-agent orchestration system for GPU clusters.
        It manages LLM inference, trading pipelines, and browser automation.

        The system consists of M1, M2, and OL1 nodes. Each node hosts multiple
        LLM models and serves inference requests. The load balancer routes
        requests based on latency and availability.

        Redis is used for state sharing, pub/sub messaging, and caching.
        All agents communicate through the message bus and can discover
        each other via the agent mesh.

        The budget tracker monitors token and cost consumption per agent.
        Alerts fire at 80% and 95% utilization thresholds.
        """
            * 3
        )

        chunks = splitter.split(sample, source="demo.txt")
        print(f"Split into {len(chunks)} chunks:")
        for c in chunks:
            print(f"  [{c.chunk_index}] tokens≈{c.token_count} chars={c.char_count}")

        print(f"\nStats: {json.dumps(splitter.stats(), indent=2)}")

    elif cmd == "file":
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/etc/os-release")
        chunks = splitter.split_file(path)
        print(f"File {path}: {len(chunks)} chunks")
        for c in chunks:
            print(f"  [{c.chunk_index}] {c.token_count} tokens: {c.text[:60]!r}...")

    elif cmd == "stats":
        print(json.dumps(splitter.stats(), indent=2))


if __name__ == "__main__":
    main()
