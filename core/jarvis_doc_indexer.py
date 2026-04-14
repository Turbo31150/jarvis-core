#!/usr/bin/env python3
"""
jarvis_doc_indexer — Document ingestion and indexing pipeline for RAG
Handles chunking, embedding, deduplication, and vector store upsert
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import aiohttp
import redis.asyncio as aioredis

log = logging.getLogger("jarvis.doc_indexer")

INDEX_FILE = Path("/home/turbo/IA/Core/jarvis/data/doc_index.jsonl")
REDIS_PREFIX = "jarvis:docidx:"


class DocStatus(str, Enum):
    PENDING = "pending"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXED = "indexed"
    FAILED = "failed"
    SKIPPED = "skipped"  # duplicate


@dataclass
class DocRecord:
    doc_id: str
    source: str
    title: str
    content_hash: str
    chunk_count: int
    char_count: int
    token_count: int
    status: DocStatus
    indexed_at: float = field(default_factory=time.time)
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "title": self.title,
            "content_hash": self.content_hash,
            "chunk_count": self.chunk_count,
            "char_count": self.char_count,
            "token_count": self.token_count,
            "status": self.status.value,
            "indexed_at": self.indexed_at,
            "error": self.error[:120] if self.error else "",
            "metadata": self.metadata,
        }


@dataclass
class IndexedChunk:
    chunk_id: str
    doc_id: str
    text: str
    embedding: list[float]
    token_count: int
    chunk_index: int
    source: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self, include_embedding: bool = False) -> dict:
        d = {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "text": self.text[:200],
            "token_count": self.token_count,
            "chunk_index": self.chunk_index,
            "source": self.source,
            "metadata": self.metadata,
            "embedding_dim": len(self.embedding),
        }
        if include_embedding:
            d["embedding"] = self.embedding
        return d


async def _embed(
    texts: list[str],
    endpoint_url: str,
    model: str,
    timeout_s: float = 30.0,
) -> list[list[float]]:
    """Call embedding endpoint, return list of vectors."""
    payload = {"input": texts, "model": model}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as sess:
            async with sess.post(f"{endpoint_url}/v1/embeddings", json=payload) as r:
                if r.status != 200:
                    text = await r.text()
                    raise RuntimeError(f"Embed HTTP {r.status}: {text[:80]}")
                data = await r.json(content_type=None)
                return [item["embedding"] for item in data.get("data", [])]
    except Exception as e:
        # Return zero vectors on failure
        log.warning(f"Embedding failed: {e}")
        return [[0.0] * 768 for _ in texts]


class DocIndexer:
    def __init__(
        self,
        embed_url: str = "http://192.168.1.26:1234",
        embed_model: str = "nomic-embed-text",
        chunk_size: int = 512,
        chunk_overlap: int = 64,
        batch_size: int = 32,
        persist: bool = True,
    ):
        self.redis: aioredis.Redis | None = None
        self.embed_url = embed_url
        self.embed_model = embed_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.batch_size = batch_size
        self._docs: dict[str, DocRecord] = {}
        self._chunks: dict[str, IndexedChunk] = {}
        self._content_hashes: set[str] = set()
        self._persist = persist
        self._stats: dict[str, int] = {
            "docs_indexed": 0,
            "docs_skipped": 0,
            "chunks_created": 0,
            "embed_calls": 0,
            "failures": 0,
        }
        if persist:
            INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _content_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _doc_id(self, source: str, title: str) -> str:
        raw = f"{source}::{title}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _chunk_text(self, text: str, source: str) -> list[tuple[str, int]]:
        """Returns [(chunk_text, approx_tokens)]."""
        chars_per_chunk = int(self.chunk_size * 4)
        step = int((self.chunk_size - self.chunk_overlap) * 4)
        chunks = []
        pos = 0
        while pos < len(text):
            end = min(pos + chars_per_chunk, len(text))
            chunk = text[pos:end].strip()
            if len(chunk) > 32:
                tok = max(1, len(chunk) // 4)
                chunks.append((chunk, tok))
            pos += step
        return chunks

    async def ingest(
        self,
        text: str,
        source: str = "",
        title: str = "",
        metadata: dict | None = None,
        force: bool = False,
    ) -> DocRecord:
        title = title or source or "untitled"
        doc_id = self._doc_id(source, title)
        content_hash = self._content_hash(text)

        # Dedup check
        if not force and content_hash in self._content_hashes:
            log.debug(f"Doc {doc_id!r} already indexed (hash match), skipping")
            self._stats["docs_skipped"] += 1
            if doc_id in self._docs:
                self._docs[doc_id].status = DocStatus.SKIPPED
                return self._docs[doc_id]
            rec = DocRecord(
                doc_id=doc_id,
                source=source,
                title=title,
                content_hash=content_hash,
                chunk_count=0,
                char_count=len(text),
                token_count=len(text) // 4,
                status=DocStatus.SKIPPED,
                metadata=metadata or {},
            )
            return rec

        rec = DocRecord(
            doc_id=doc_id,
            source=source,
            title=title,
            content_hash=content_hash,
            chunk_count=0,
            char_count=len(text),
            token_count=len(text) // 4,
            status=DocStatus.CHUNKING,
            metadata=metadata or {},
        )
        self._docs[doc_id] = rec

        try:
            # Chunk
            raw_chunks = self._chunk_text(text, source)
            rec.chunk_count = len(raw_chunks)
            rec.status = DocStatus.EMBEDDING

            # Embed in batches
            all_embeddings: list[list[float]] = []
            for i in range(0, len(raw_chunks), self.batch_size):
                batch_texts = [c[0] for c in raw_chunks[i : i + self.batch_size]]
                embeddings = await _embed(batch_texts, self.embed_url, self.embed_model)
                all_embeddings.extend(embeddings)
                self._stats["embed_calls"] += 1

            # Store chunks
            for idx, ((chunk_text, tok), emb) in enumerate(
                zip(raw_chunks, all_embeddings)
            ):
                chunk_id = f"{doc_id}:{idx}"
                self._chunks[chunk_id] = IndexedChunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    text=chunk_text,
                    embedding=emb,
                    token_count=tok,
                    chunk_index=idx,
                    source=source,
                    metadata=metadata or {},
                )
                self._stats["chunks_created"] += 1

            self._content_hashes.add(content_hash)
            rec.status = DocStatus.INDEXED
            rec.indexed_at = time.time()
            self._stats["docs_indexed"] += 1

            if self._persist:
                try:
                    with open(INDEX_FILE, "a") as f:
                        f.write(json.dumps(rec.to_dict()) + "\n")
                except Exception:
                    pass

            if self.redis:
                asyncio.create_task(self._redis_record(rec))

            log.info(f"Indexed {doc_id!r}: {len(raw_chunks)} chunks from {source!r}")

        except Exception as e:
            rec.status = DocStatus.FAILED
            rec.error = str(e)[:200]
            self._stats["failures"] += 1
            log.error(f"Failed to index {doc_id!r}: {e}")

        return rec

    async def ingest_many(
        self,
        docs: list[dict],  # [{text, source, title, metadata}]
        concurrency: int = 4,
    ) -> list[DocRecord]:
        sem = asyncio.Semaphore(concurrency)

        async def bounded(doc: dict) -> DocRecord:
            async with sem:
                return await self.ingest(
                    doc.get("text", ""),
                    source=doc.get("source", ""),
                    title=doc.get("title", ""),
                    metadata=doc.get("metadata"),
                )

        return list(await asyncio.gather(*[bounded(d) for d in docs]))

    async def _redis_record(self, rec: DocRecord):
        if not self.redis:
            return
        try:
            await self.redis.hset(
                f"{REDIS_PREFIX}docs",
                rec.doc_id,
                json.dumps(rec.to_dict()),
            )
        except Exception:
            pass

    def get_chunks(self, doc_id: str) -> list[IndexedChunk]:
        return [c for c in self._chunks.values() if c.doc_id == doc_id]

    def list_docs(self) -> list[dict]:
        return [r.to_dict() for r in self._docs.values()]

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_docs": len(self._docs),
            "total_chunks": len(self._chunks),
        }


def build_jarvis_doc_indexer(
    embed_url: str = "http://192.168.1.26:1234",
    embed_model: str = "nomic-embed-text",
) -> DocIndexer:
    return DocIndexer(
        embed_url=embed_url, embed_model=embed_model, chunk_size=512, persist=True
    )


async def main():
    import sys

    indexer = build_jarvis_doc_indexer()
    await indexer.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        docs = [
            {
                "text": "JARVIS is a multi-agent orchestration system. " * 40,
                "source": "jarvis-overview.txt",
                "title": "JARVIS Overview",
            },
            {
                "text": "Redis is used for pub/sub messaging and state caching. " * 30,
                "source": "redis-notes.txt",
                "title": "Redis Notes",
            },
        ]
        print(f"Indexing {len(docs)} documents...")
        records = await indexer.ingest_many(docs)
        for r in records:
            icon = {"indexed": "✅", "skipped": "⏭", "failed": "❌"}.get(
                r.status.value, "?"
            )
            print(f"  {icon} {r.doc_id} {r.title!r} chunks={r.chunk_count}")

        print(f"\nStats: {json.dumps(indexer.stats(), indent=2)}")

    elif cmd == "list":
        for d in indexer.list_docs():
            print(json.dumps(d))

    elif cmd == "stats":
        print(json.dumps(indexer.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
