#!/usr/bin/env python3
"""
jarvis_sparse_retriever — BM25-style sparse retrieval for keyword search in RAG
In-memory inverted index with TF-IDF / BM25 scoring
"""

import json
import logging
import math
import re
import string
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jarvis.sparse_retriever")

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "what",
        "which",
        "who",
        "whom",
        "not",
        "no",
        "nor",
        "so",
    }
)

_PUNC_RE = re.compile(r"[" + re.escape(string.punctuation) + r"]")


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    text = _PUNC_RE.sub(" ", text)
    tokens = [t for t in text.split() if t and t not in STOPWORDS and len(t) > 1]
    return tokens


@dataclass
class Document:
    doc_id: str
    text: str
    source: str = ""
    metadata: dict = field(default_factory=dict)
    _tokens: list[str] = field(default_factory=list, repr=False)
    _tf: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._tokens = _tokenize(self.text)
        n = max(len(self._tokens), 1)
        freq: dict[str, int] = {}
        for t in self._tokens:
            freq[t] = freq.get(t, 0) + 1
        self._tf = {t: c / n for t, c in freq.items()}

    @property
    def length(self) -> int:
        return len(self._tokens)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "text_preview": self.text[:100],
            "token_count": self.length,
            "metadata": self.metadata,
        }


@dataclass
class RetrievalResult:
    doc_id: str
    score: float
    text: str
    source: str = ""
    metadata: dict = field(default_factory=dict)
    matched_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "score": round(self.score, 6),
            "text": self.text[:300],
            "source": self.source,
            "matched_terms": self.matched_terms,
            "metadata": self.metadata,
        }


class SparseRetriever:
    """
    BM25 sparse retriever with in-memory inverted index.

    BM25 formula:
        score(q,d) = Σ IDF(qi) × (tf(qi,d) × (k1+1)) / (tf(qi,d) + k1 × (1 - b + b × |d|/avgdl))
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: dict[str, Document] = {}
        # Inverted index: term → {doc_id: term_freq}
        self._index: dict[str, dict[str, int]] = {}
        self._df: dict[str, int] = {}  # document frequency per term
        self._avg_dl: float = 0.0
        self._stats: dict[str, int] = {
            "docs_indexed": 0,
            "queries": 0,
        }

    def index(
        self, doc_id: str, text: str, source: str = "", metadata: dict | None = None
    ):
        doc = Document(doc_id=doc_id, text=text, source=source, metadata=metadata or {})
        # Remove old version if re-indexing
        if doc_id in self._docs:
            self._remove_doc(doc_id)

        self._docs[doc_id] = doc
        self._stats["docs_indexed"] += 1

        # Update inverted index
        for token, tf in doc._tf.items():
            count = sum(1 for t in doc._tokens if t == token)
            self._index.setdefault(token, {})[doc_id] = count
            self._df[token] = self._df.get(token, 0) + 1

        self._update_avg_dl()

    def index_many(self, docs: list[dict]):
        for d in docs:
            self.index(
                d["doc_id"],
                d.get("text", ""),
                source=d.get("source", ""),
                metadata=d.get("metadata"),
            )

    def _remove_doc(self, doc_id: str):
        doc = self._docs.pop(doc_id, None)
        if not doc:
            return
        for token in set(doc._tokens):
            if token in self._index:
                self._index[token].pop(doc_id, None)
                if not self._index[token]:
                    del self._index[token]
                    del self._df[token]
                else:
                    self._df[token] = max(0, self._df.get(token, 1) - 1)

    def _update_avg_dl(self):
        if not self._docs:
            self._avg_dl = 0.0
            return
        self._avg_dl = sum(d.length for d in self._docs.values()) / len(self._docs)

    def _idf(self, term: str) -> float:
        n = len(self._docs)
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log((n - df + 0.5) / (df + 0.5) + 1)

    def _bm25_score(
        self, doc: Document, query_terms: list[str]
    ) -> tuple[float, list[str]]:
        score = 0.0
        matched = []
        avg_dl = max(self._avg_dl, 1.0)
        for term in query_terms:
            tf = doc._tokens.count(term)
            if tf == 0:
                continue
            matched.append(term)
            idf = self._idf(term)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * doc.length / avg_dl)
            score += idf * numerator / denominator
        return score, list(set(matched))

    def search(
        self,
        query: str,
        top_k: int = 10,
        filter_fn: Any = None,  # callable(doc) → bool
        min_score: float = 0.0,
    ) -> list[RetrievalResult]:
        self._stats["queries"] += 1
        query_terms = _tokenize(query)
        if not query_terms:
            return []

        # Candidate set: docs that contain at least one query term
        candidates: set[str] = set()
        for term in query_terms:
            candidates.update(self._index.get(term, {}).keys())

        results: list[RetrievalResult] = []
        for doc_id in candidates:
            doc = self._docs.get(doc_id)
            if not doc:
                continue
            if filter_fn and not filter_fn(doc):
                continue
            score, matched = self._bm25_score(doc, query_terms)
            if score >= min_score:
                results.append(
                    RetrievalResult(
                        doc_id=doc_id,
                        score=score,
                        text=doc.text,
                        source=doc.source,
                        metadata=doc.metadata,
                        matched_terms=matched,
                    )
                )

        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def get_doc(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    def delete(self, doc_id: str):
        self._remove_doc(doc_id)
        self._update_avg_dl()

    def stats(self) -> dict:
        return {
            **self._stats,
            "total_docs": len(self._docs),
            "vocab_size": len(self._index),
            "avg_doc_length": round(self._avg_dl, 1),
            "k1": self.k1,
            "b": self.b,
        }


def build_jarvis_sparse_retriever() -> SparseRetriever:
    retriever = SparseRetriever(k1=1.5, b=0.75)
    return retriever


def main():
    import sys

    retriever = build_jarvis_sparse_retriever()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        # Index some documents
        corpus = [
            {
                "doc_id": "d1",
                "text": "JARVIS is a multi-agent orchestration system for GPU clusters and LLM inference.",
                "source": "overview",
            },
            {
                "doc_id": "d2",
                "text": "Redis enables pub/sub messaging, state caching, and distributed coordination.",
                "source": "redis",
            },
            {
                "doc_id": "d3",
                "text": "Trading agents monitor crypto markets and execute strategies using technical analysis.",
                "source": "trading",
            },
            {
                "doc_id": "d4",
                "text": "The load balancer routes inference requests across M1, M2, and OL1 GPU nodes.",
                "source": "infra",
            },
            {
                "doc_id": "d5",
                "text": "Budget tracking monitors token consumption and cost per agent with daily limits.",
                "source": "budget",
            },
            {
                "doc_id": "d6",
                "text": "Circuit breakers protect backends from cascading failures with automatic recovery.",
                "source": "reliability",
            },
        ]
        retriever.index_many(corpus)
        print(
            f"Indexed {len(corpus)} documents (vocab={retriever.stats()['vocab_size']})\n"
        )

        queries = [
            "GPU inference cluster",
            "trading crypto strategy",
            "cost budget tokens",
        ]
        for q in queries:
            results = retriever.search(q, top_k=3)
            print(f"Query: {q!r}")
            for r in results:
                print(
                    f"  [{r.doc_id}] score={r.score:.3f} matched={r.matched_terms} | {r.text[:60]}"
                )
            print()

        print(f"Stats: {json.dumps(retriever.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(retriever.stats(), indent=2))


if __name__ == "__main__":
    main()
