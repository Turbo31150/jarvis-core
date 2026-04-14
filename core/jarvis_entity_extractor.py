#!/usr/bin/env python3
"""
jarvis_entity_extractor — Rule-based and pattern entity extraction from text
Extracts IPs, models, nodes, tickers, commands, and custom entity types
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.entity_extractor")


class EntityType(str, Enum):
    IP_ADDRESS = "ip_address"
    HOSTNAME = "hostname"
    PORT = "port"
    MODEL_NAME = "model_name"
    CLUSTER_NODE = "cluster_node"
    TICKER = "ticker"  # crypto/stock ticker
    PRICE = "price"
    PERCENTAGE = "percentage"
    COMMAND = "command"
    FILE_PATH = "file_path"
    URL = "url"
    EMAIL = "email"
    AGENT_ID = "agent_id"
    GPU = "gpu"
    DURATION = "duration"  # e.g. "5 minutes", "2h"
    CUSTOM = "custom"


@dataclass
class Entity:
    entity_id: str
    entity_type: EntityType
    value: str
    raw_text: str
    start: int
    end: int
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "type": self.entity_type.value,
            "value": self.value,
            "raw_text": self.raw_text,
            "span": [self.start, self.end],
            "confidence": round(self.confidence, 3),
            "metadata": self.metadata,
        }


@dataclass
class ExtractionPattern:
    pattern_id: str
    entity_type: EntityType
    regex: re.Pattern
    value_group: int = 0  # which capture group is the value
    normalizer: Any = None  # callable(raw) → normalized_value
    confidence: float = 1.0


def _norm_ip(v: str) -> str:
    return v.strip()


def _norm_upper(v: str) -> str:
    return v.upper().strip()


def _norm_lower(v: str) -> str:
    return v.lower().strip()


def _norm_price(v: str) -> str:
    return v.replace(",", "").replace("$", "").strip()


_DEFAULT_PATTERNS: list[tuple[str, EntityType, str, int, Any]] = [
    # (pattern_id, type, regex, group, normalizer)
    (
        "ipv4",
        EntityType.IP_ADDRESS,
        r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b",
        1,
        _norm_ip,
    ),
    ("port", EntityType.PORT, r"\bport\s+(\d{2,5})\b|\b:(\d{2,5})\b", 1, None),
    ("url", EntityType.URL, r"https?://[^\s\"'<>]+", 0, _norm_lower),
    (
        "email",
        EntityType.EMAIL,
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
        0,
        _norm_lower,
    ),
    (
        "filepath_unix",
        EntityType.FILE_PATH,
        r"(?:^|[\s\"'])(/(?:home|etc|usr|var|tmp|opt)/[^\s\"']+)",
        1,
        None,
    ),
    (
        "model_claude",
        EntityType.MODEL_NAME,
        r"\b(claude[-\s](?:opus|sonnet|haiku)[-\s]?\d*[\w.]*)\b",
        1,
        _norm_lower,
    ),
    (
        "model_gpt",
        EntityType.MODEL_NAME,
        r"\b(gpt-4[o\w\-]*|gpt-3\.5[\w\-]*)\b",
        1,
        _norm_lower,
    ),
    ("model_qwen", EntityType.MODEL_NAME, r"\b(qwen[\d.]+[-\w]*)\b", 1, _norm_lower),
    ("model_deepseek", EntityType.MODEL_NAME, r"\b(deepseek[-\w]+)\b", 1, _norm_lower),
    (
        "cluster_node",
        EntityType.CLUSTER_NODE,
        r"\b(m[123]|ol1|redis|gateway)\b",
        1,
        _norm_lower,
    ),
    (
        "gpu_rtx",
        EntityType.GPU,
        r"\b(rtx\s*\d{3,4}[a-z\s]*(?:ti|super)?|gtx\s*\d{3,4}[a-z\s]*|rx\s*\d{4}[a-z\s]*xt?)\b",
        1,
        _norm_lower,
    ),
    (
        "ticker_crypto",
        EntityType.TICKER,
        r"\b(BTC|ETH|SOL|DOGE|ADA|XRP|MATIC|AVAX|DOT|LINK)(?:/USDT|/USD|/BTC)?\b",
        1,
        _norm_upper,
    ),
    ("price_usd", EntityType.PRICE, r"\$\s*([\d,]+(?:\.\d{1,4})?)", 1, _norm_price),
    ("percentage", EntityType.PERCENTAGE, r"(\d+(?:\.\d+)?)\s*%", 1, None),
    (
        "duration_h",
        EntityType.DURATION,
        r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b",
        1,
        None,
    ),
    ("duration_m", EntityType.DURATION, r"\b(\d+)\s*(?:minutes?|mins?|m)\b", 1, None),
    ("duration_s", EntityType.DURATION, r"\b(\d+)\s*(?:seconds?|secs?|s)\b", 1, None),
    (
        "agent_id",
        EntityType.AGENT_ID,
        r"\b([\w]+-agent|inference-gw|trading-agent|monitor-[\w]+|admin-[\w]+)\b",
        1,
        _norm_lower,
    ),
    ("command_jarvis", EntityType.COMMAND, r"\b(jarvis(?:-\w+)?)\s+([\w]+)", 0, None),
]


class EntityExtractor:
    def __init__(self):
        self._patterns: list[ExtractionPattern] = []
        self._custom: list[ExtractionPattern] = []
        self._stats: dict[str, int] = {"extractions": 0, "entities_found": 0}
        self._load_defaults()

    def _load_defaults(self):
        for pid, etype, pattern, group, normalizer in _DEFAULT_PATTERNS:
            try:
                self._patterns.append(
                    ExtractionPattern(
                        pattern_id=pid,
                        entity_type=etype,
                        regex=re.compile(pattern, re.IGNORECASE),
                        value_group=group,
                        normalizer=normalizer,
                    )
                )
            except re.error as e:
                log.warning(f"Pattern {pid!r} compile error: {e}")

    def add_pattern(
        self,
        pattern_id: str,
        entity_type: EntityType,
        regex: str,
        value_group: int = 0,
        normalizer: Any = None,
        confidence: float = 0.9,
    ):
        self._custom.append(
            ExtractionPattern(
                pattern_id=pattern_id,
                entity_type=entity_type,
                regex=re.compile(regex, re.IGNORECASE),
                value_group=value_group,
                normalizer=normalizer,
                confidence=confidence,
            )
        )

    def extract(
        self,
        text: str,
        types: list[EntityType] | None = None,
    ) -> list[Entity]:
        self._stats["extractions"] += 1
        entities: list[Entity] = []
        seen_spans: set[tuple[int, int]] = set()

        all_patterns = self._patterns + self._custom
        if types:
            all_patterns = [p for p in all_patterns if p.entity_type in types]

        for pat in all_patterns:
            for m in pat.regex.finditer(text):
                start, end = m.start(), m.end()

                # Try to get value from specified group
                try:
                    raw = m.group(pat.value_group) if pat.value_group else m.group(0)
                except IndexError:
                    raw = m.group(0)

                if not raw:
                    continue

                # Deduplicate overlapping spans
                span_key = (start, end)
                if span_key in seen_spans:
                    continue

                # Check if this span overlaps with existing higher-confidence entity
                overlapping = any(
                    not (end <= es or start >= ee) for es, ee in seen_spans
                )
                if overlapping:
                    continue

                seen_spans.add(span_key)

                value = pat.normalizer(raw) if pat.normalizer else raw.strip()
                entity_id = f"{pat.entity_type.value}:{len(entities)}"

                entities.append(
                    Entity(
                        entity_id=entity_id,
                        entity_type=pat.entity_type,
                        value=value,
                        raw_text=raw,
                        start=start,
                        end=end,
                        confidence=pat.confidence,
                    )
                )

        self._stats["entities_found"] += len(entities)
        return sorted(entities, key=lambda e: e.start)

    def extract_dict(
        self,
        text: str,
        types: list[EntityType] | None = None,
    ) -> dict[str, list[str]]:
        """Returns {entity_type: [value, ...]} grouped dict."""
        result: dict[str, list[str]] = {}
        for ent in self.extract(text, types):
            result.setdefault(ent.entity_type.value, []).append(ent.value)
        return result

    def extract_first(self, text: str, entity_type: EntityType) -> str | None:
        for ent in self.extract(text, [entity_type]):
            return ent.value
        return None

    def stats(self) -> dict:
        return {
            **self._stats,
            "patterns": len(self._patterns),
            "custom_patterns": len(self._custom),
        }


def build_jarvis_entity_extractor() -> EntityExtractor:
    return EntityExtractor()


def main():
    import sys

    extractor = build_jarvis_entity_extractor()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        texts = [
            "Please check M1 at 192.168.1.85:1234 running qwen3.5-27b — GPU temp is 72%",
            "Trading BTC/USDT hit $67,500 (+3.2%), signal from trading-agent via deepseek-r1",
            "SSH into ol1 and run jarvis-gpu status, log to /var/log/jarvis/gpu.log",
            "claude-sonnet-4-6 latency p95=1200ms vs gpt-4o at $5.00/1M tokens",
        ]
        for text in texts:
            print(f"\nText: {text!r}")
            entities = extractor.extract(text)
            for e in entities:
                print(f"  [{e.entity_type.value:<15}] {e.value!r}")

        print(f"\nStats: {json.dumps(extractor.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(extractor.stats(), indent=2))


if __name__ == "__main__":
    main()
