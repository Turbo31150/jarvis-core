#!/usr/bin/env python3
"""
jarvis_topic_extractor — Topic, entity, and keyword extraction
TF-IDF based ranking, named entity detection, topic clustering, keyword graph.
"""

import asyncio
import json
import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.topic_extractor")


class EntityType(str, Enum):
    PERSON = "person"
    ORGANIZATION = "org"
    LOCATION = "location"
    TECHNOLOGY = "technology"
    DATE = "date"
    NUMBER = "number"
    CONCEPT = "concept"
    UNKNOWN = "unknown"


@dataclass
class Keyword:
    text: str
    score: float
    count: int = 1
    entity_type: EntityType = EntityType.UNKNOWN
    positions: list[int] = field(default_factory=list)  # char offsets

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "score": round(self.score, 4),
            "count": self.count,
            "type": self.entity_type.value,
        }


@dataclass
class Topic:
    topic_id: str
    label: str
    keywords: list[str]
    score: float
    document_count: int = 0

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "keywords": self.keywords[:5],
            "score": round(self.score, 4),
        }


@dataclass
class ExtractionResult:
    text_id: str
    keywords: list[Keyword]
    entities: list[Keyword]
    topics: list[Topic]
    summary_tags: list[str]
    duration_ms: float = 0.0

    def top_keywords(self, n: int = 10) -> list[str]:
        return [k.text for k in sorted(self.keywords, key=lambda x: -x.score)[:n]]

    def to_dict(self) -> dict:
        return {
            "text_id": self.text_id,
            "keywords": [k.to_dict() for k in self.keywords[:15]],
            "entities": [e.to_dict() for e in self.entities[:10]],
            "topics": [t.to_dict() for t in self.topics],
            "summary_tags": self.summary_tags,
            "duration_ms": round(self.duration_ms, 2),
        }


# --- Stopwords ---
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "not",
    "no", "nor", "so", "yet", "both", "either", "neither", "each", "few",
    "more", "most", "other", "some", "such", "than", "too", "very", "just",
    "that", "this", "these", "those", "i", "you", "he", "she", "it", "we",
    "they", "what", "which", "who", "when", "where", "why", "how",
    "also", "about", "into", "through", "during", "before", "after",
}

# --- Entity patterns ---
_DATE_RE = re.compile(
    r"\b(\d{4}[-/]\d{2}[-/]\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?(?:%|k|m|b|ms|s|gb|mb|kb)?\b", re.IGNORECASE)
_TECH_KEYWORDS = {
    "gpu", "cpu", "ram", "vram", "api", "llm", "ai", "ml", "redis", "docker",
    "kubernetes", "python", "javascript", "typescript", "rust", "cuda", "model",
    "inference", "embedding", "token", "transformer", "neural", "network",
    "cluster", "node", "server", "database", "sql", "nosql", "cache", "queue",
    "pipeline", "async", "sync", "http", "grpc", "websocket", "json", "yaml",
}
_ORG_SUFFIXES = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s+(Inc|Corp|Ltd|LLC|GmbH|SAS|SA|AG)\b"
)
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b")


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"[^\w\s-]", " ", text.lower())
    return [t for t in text.split() if len(t) > 2 and t not in _STOPWORDS]


def _sentence_split(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?\n]+", text) if len(s.strip()) > 5]


class TFIDF:
    """Lightweight TF-IDF over an in-memory document corpus."""

    def __init__(self):
        self._docs: list[list[str]] = []
        self._idf: dict[str, float] = {}

    def add_document(self, tokens: list[str]):
        self._docs.append(tokens)
        self._idf = {}  # invalidate

    def _compute_idf(self):
        n = max(len(self._docs), 1)
        df: Counter = Counter()
        for doc in self._docs:
            for term in set(doc):
                df[term] += 1
        self._idf = {term: math.log((n + 1) / (count + 1)) + 1 for term, count in df.items()}

    def score(self, tokens: list[str]) -> dict[str, float]:
        if not self._idf:
            self._compute_idf()
        tf = Counter(tokens)
        n = max(len(tokens), 1)
        scores = {}
        for term, count in tf.items():
            tf_score = count / n
            idf_score = self._idf.get(term, math.log(2) + 1)
            scores[term] = tf_score * idf_score
        return scores


class TopicExtractor:
    """
    Extracts keywords, named entities, and topics from text.
    Uses TF-IDF + pattern matching for entity detection.
    """

    def __init__(self, tfidf_corpus: list[str] | None = None):
        self._tfidf = TFIDF()
        self._results: list[ExtractionResult] = []
        self._stats: dict[str, Any] = {
            "texts_processed": 0,
            "keywords_extracted": 0,
            "entities_found": 0,
        }
        # Seed TF-IDF with corpus if provided
        if tfidf_corpus:
            for doc in tfidf_corpus:
                self._tfidf.add_document(_tokenize(doc))

    def _new_id(self) -> str:
        import secrets
        return secrets.token_hex(5)

    def _classify_entity(self, token: str, original: str) -> EntityType:
        low = token.lower()
        if low in _TECH_KEYWORDS:
            return EntityType.TECHNOLOGY
        if _DATE_RE.match(original):
            return EntityType.DATE
        if _NUMBER_RE.fullmatch(original):
            return EntityType.NUMBER
        if original[0].isupper() and len(original) > 2:
            return EntityType.CONCEPT
        return EntityType.UNKNOWN

    def _extract_entities(self, text: str) -> list[Keyword]:
        entities = []

        # Tech terms
        tokens = text.lower().split()
        for tok in tokens:
            clean = re.sub(r"\W", "", tok)
            if clean in _TECH_KEYWORDS:
                entities.append(Keyword(
                    text=clean,
                    score=0.9,
                    count=tokens.count(tok),
                    entity_type=EntityType.TECHNOLOGY,
                ))

        # Proper nouns (capitalized sequences)
        for m in _PROPER_NOUN_RE.finditer(text):
            phrase = m.group(0)
            if phrase.lower() not in _STOPWORDS:
                entities.append(Keyword(
                    text=phrase,
                    score=0.75,
                    entity_type=EntityType.PERSON,
                    positions=[m.start()],
                ))

        # Organizations
        for m in _ORG_SUFFIXES.finditer(text):
            entities.append(Keyword(
                text=m.group(0),
                score=0.85,
                entity_type=EntityType.ORGANIZATION,
                positions=[m.start()],
            ))

        # Dates
        for m in _DATE_RE.finditer(text):
            entities.append(Keyword(
                text=m.group(0),
                score=0.7,
                entity_type=EntityType.DATE,
                positions=[m.start()],
            ))

        # Deduplicate by text (keep highest score)
        seen: dict[str, Keyword] = {}
        for e in entities:
            key = e.text.lower()
            if key not in seen or e.score > seen[key].score:
                seen[key] = e
        return list(seen.values())

    def _infer_topics(self, keywords: list[Keyword]) -> list[Topic]:
        """Simple rule-based topic grouping."""
        TOPIC_RULES: dict[str, list[str]] = {
            "infrastructure": ["gpu", "cpu", "cluster", "node", "server", "docker", "kubernetes"],
            "ai_ml": ["model", "llm", "inference", "embedding", "neural", "transformer", "ai", "ml"],
            "performance": ["latency", "throughput", "benchmark", "speed", "optimization", "cache"],
            "data": ["database", "sql", "redis", "pipeline", "etl", "vector", "index"],
            "security": ["auth", "token", "key", "secret", "permission", "access", "ssl", "tls"],
            "development": ["python", "api", "code", "function", "async", "test", "deploy"],
            "trading": ["price", "btc", "eth", "market", "signal", "trade", "portfolio"],
        }

        kw_texts = {k.text.lower() for k in keywords}
        topics = []
        for topic_name, terms in TOPIC_RULES.items():
            matches = [t for t in terms if t in kw_texts]
            if len(matches) >= 2:
                topics.append(Topic(
                    topic_id=topic_name,
                    label=topic_name.replace("_", " ").title(),
                    keywords=matches,
                    score=len(matches) / len(terms),
                ))
        return sorted(topics, key=lambda t: -t.score)

    def extract(self, text: str, text_id: str | None = None) -> ExtractionResult:
        t0 = time.time()
        tid = text_id or self._new_id()
        tokens = _tokenize(text)
        self._tfidf.add_document(tokens)

        # TF-IDF scores
        scores = self._tfidf.score(tokens)
        freq = Counter(tokens)

        keywords = []
        for term, score in sorted(scores.items(), key=lambda x: -x[1])[:50]:
            kw = Keyword(
                text=term,
                score=score,
                count=freq[term],
                entity_type=self._classify_entity(term, term),
            )
            keywords.append(kw)

        entities = self._extract_entities(text)
        topics = self._infer_topics(keywords + entities)

        # Summary tags: top 5 unique keywords + entity labels
        summary_tags = list(dict.fromkeys(
            [k.text for k in keywords[:5]] + [e.text for e in entities[:3]]
        ))[:8]

        result = ExtractionResult(
            text_id=tid,
            keywords=keywords,
            entities=entities,
            topics=topics,
            summary_tags=summary_tags,
            duration_ms=(time.time() - t0) * 1000,
        )
        self._results.append(result)
        self._stats["texts_processed"] += 1
        self._stats["keywords_extracted"] += len(keywords)
        self._stats["entities_found"] += len(entities)
        return result

    def batch_extract(self, texts: list[str]) -> list[ExtractionResult]:
        return [self.extract(t) for t in texts]

    def global_keywords(self, top_n: int = 20) -> list[dict]:
        """Aggregate keywords across all processed texts."""
        counter: Counter = Counter()
        for r in self._results:
            for kw in r.keywords:
                counter[kw.text] += kw.count
        return [{"keyword": k, "count": v} for k, v in counter.most_common(top_n)]

    def stats(self) -> dict:
        return {**self._stats, "cached_results": len(self._results)}


def build_jarvis_topic_extractor(corpus: list[str] | None = None) -> TopicExtractor:
    return TopicExtractor(tfidf_corpus=corpus)


async def main():
    import sys
    extractor = build_jarvis_topic_extractor()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Topic extractor demo...\n")
        texts = [
            "The JARVIS cluster runs on GPU nodes M1 and M2 using LLM models like Qwen3.5 and DeepSeek-R1 for AI inference.",
            "Redis cache optimization improved latency by 40% on the inference pipeline benchmark.",
            "Anthropic Claude 4.6 achieves state-of-the-art performance on reasoning tasks with transformer architecture.",
            "Trading signals from BTC price analysis using RSI and Bollinger Bands on the MEXC futures market.",
            "Python async pipeline for ETL processing with SQL storage in SQLite and Pinecone vector database.",
        ]
        for text in texts:
            result = extractor.extract(text)
            print(f"  [{result.text_id}] {text[:60]}")
            print(f"    Keywords: {', '.join(result.top_keywords(6))}")
            print(f"    Entities: {[e.text+'/'+e.entity_type.value for e in result.entities[:4]]}")
            print(f"    Topics:   {[t.label for t in result.topics]}")
            print(f"    Tags:     {result.summary_tags}")
            print()

        print("  Global top keywords:")
        for kw in extractor.global_keywords(8):
            print(f"    {kw['keyword']:<20} count={kw['count']}")

        print(f"\nStats: {json.dumps(extractor.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(extractor.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
