#!/usr/bin/env python3
"""
jarvis_output_parser — LLM output parsing: JSON extraction, structured data, retry on parse failure
Handles malformed JSON, markdown code blocks, mixed text+data responses
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.output_parser")


class ParseStrategy(str, Enum):
    JSON_STRICT = "json_strict"  # pure JSON, fail if not
    JSON_LENIENT = "json_lenient"  # extract JSON from mixed text
    JSON_REPAIR = "json_repair"  # attempt to fix malformed JSON
    MARKDOWN_CODE = "markdown_code"  # extract from ```json...``` blocks
    KEY_VALUE = "key_value"  # parse "Key: Value" lines
    LIST = "list"  # extract bulleted/numbered list
    REGEX = "regex"  # custom regex extraction


class ParseStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"  # got something but may be incomplete
    FAILED = "failed"
    EMPTY = "empty"


@dataclass
class ParseResult:
    status: ParseStatus
    data: Any = None
    raw: str = ""
    strategy_used: ParseStrategy = ParseStrategy.JSON_LENIENT
    confidence: float = 1.0
    errors: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.status in (ParseStatus.SUCCESS, ParseStatus.PARTIAL)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "data": self.data,
            "strategy": self.strategy_used.value,
            "confidence": round(self.confidence, 3),
            "errors": self.errors,
        }


# --- Low-level parsing helpers ---


def _extract_json_objects(text: str) -> list[dict | list]:
    """Find all valid JSON objects/arrays in arbitrary text."""
    results = []
    i = 0
    while i < len(text):
        if text[i] in ("{", "["):
            for end in range(len(text), i, -1):
                chunk = text[i:end]
                try:
                    obj = json.loads(chunk)
                    results.append(obj)
                    i = end
                    break
                except Exception:
                    continue
            else:
                i += 1
        else:
            i += 1
    return results


def _extract_code_blocks(text: str) -> list[str]:
    """Extract content from ```...``` blocks."""
    # json/python/yaml tagged blocks
    tagged = re.findall(r"```(?:json|python|yaml|text)?\s*\n?(.*?)```", text, re.DOTALL)
    # bare blocks
    bare = re.findall(r"```(.*?)```", text, re.DOTALL)
    return [b.strip() for b in tagged + bare if b.strip()]


def _repair_json(text: str) -> str:
    """Best-effort JSON repair: trailing commas, single quotes, missing quotes."""
    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Replace single quotes with double quotes (naive)
    text = re.sub(r"(?<![\\])'", '"', text)
    # Fix unquoted keys
    text = re.sub(r"(\{|,)\s*([a-zA-Z_]\w*)\s*:", r'\1 "\2":', text)
    # Remove Python-style True/False/None
    text = (
        text.replace("True", "true").replace("False", "false").replace("None", "null")
    )
    # Remove trailing content after closing brace
    match = re.search(r"^(\{.*\}|\[.*\])\s*", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def _parse_key_value(text: str) -> dict[str, str]:
    """Parse 'Key: Value' lines into dict."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w\s]*?):\s*(.+)$", line.strip())
        if m:
            key = re.sub(r"\s+", "_", m.group(1).strip().lower())
            result[key] = m.group(2).strip()
    return result


def _parse_list(text: str) -> list[str]:
    """Extract bulleted or numbered list items."""
    items = re.findall(r"^[\s]*(?:\d+[\.\):]|[-*•])\s+(.+)$", text, re.MULTILINE)
    return [item.strip() for item in items]


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# --- Main parser ---


class OutputParser:
    def __init__(self, default_strategy: ParseStrategy = ParseStrategy.JSON_LENIENT):
        self._default_strategy = default_strategy
        self._stats: dict[str, int] = {
            "parsed": 0,
            "success": 0,
            "partial": 0,
            "failed": 0,
            "repaired": 0,
        }

    def parse(
        self,
        text: str,
        strategy: ParseStrategy | None = None,
        schema: dict | None = None,  # optional expected keys for validation
        regex_pattern: str | None = None,
    ) -> ParseResult:
        self._stats["parsed"] += 1
        strategy = strategy or self._default_strategy
        text = _strip_think(text).strip()

        if not text:
            self._stats["failed"] += 1
            return ParseResult(ParseStatus.EMPTY, raw=text, strategy_used=strategy)

        result = self._dispatch(text, strategy, regex_pattern)

        # Schema validation
        if result.ok and schema and isinstance(result.data, dict):
            missing = [k for k in schema if k not in result.data]
            if missing:
                result.errors.append(f"missing keys: {missing}")
                result.confidence *= 0.7
                result.status = ParseStatus.PARTIAL

        self._stats[
            result.status.value if result.status.value in self._stats else "failed"
        ] += 1
        return result

    def _dispatch(
        self, text: str, strategy: ParseStrategy, regex_pattern: str | None
    ) -> ParseResult:
        if strategy == ParseStrategy.JSON_STRICT:
            return self._parse_strict(text)

        elif strategy == ParseStrategy.JSON_LENIENT:
            return self._parse_lenient(text)

        elif strategy == ParseStrategy.JSON_REPAIR:
            return self._parse_repair(text)

        elif strategy == ParseStrategy.MARKDOWN_CODE:
            return self._parse_code_blocks(text)

        elif strategy == ParseStrategy.KEY_VALUE:
            data = _parse_key_value(text)
            if data:
                return ParseResult(
                    ParseStatus.SUCCESS, data=data, raw=text, strategy_used=strategy
                )
            return ParseResult(
                ParseStatus.FAILED,
                raw=text,
                strategy_used=strategy,
                errors=["no key:value pairs found"],
            )

        elif strategy == ParseStrategy.LIST:
            items = _parse_list(text)
            if items:
                return ParseResult(
                    ParseStatus.SUCCESS, data=items, raw=text, strategy_used=strategy
                )
            return ParseResult(
                ParseStatus.FAILED,
                raw=text,
                strategy_used=strategy,
                errors=["no list items found"],
            )

        elif strategy == ParseStrategy.REGEX:
            if not regex_pattern:
                return ParseResult(
                    ParseStatus.FAILED,
                    raw=text,
                    strategy_used=strategy,
                    errors=["no regex_pattern provided"],
                )
            match = re.search(regex_pattern, text, re.DOTALL)
            if match:
                data = match.groupdict() or {"match": match.group(0)}
                return ParseResult(
                    ParseStatus.SUCCESS, data=data, raw=text, strategy_used=strategy
                )
            return ParseResult(
                ParseStatus.FAILED,
                raw=text,
                strategy_used=strategy,
                errors=["regex did not match"],
            )

        return self._parse_lenient(text)

    def _parse_strict(self, text: str) -> ParseResult:
        try:
            data = json.loads(text)
            return ParseResult(
                ParseStatus.SUCCESS,
                data=data,
                raw=text,
                strategy_used=ParseStrategy.JSON_STRICT,
            )
        except json.JSONDecodeError as e:
            return ParseResult(
                ParseStatus.FAILED,
                raw=text,
                strategy_used=ParseStrategy.JSON_STRICT,
                errors=[str(e)],
                confidence=0.0,
            )

    def _parse_lenient(self, text: str) -> ParseResult:
        # 1. Try strict
        try:
            data = json.loads(text)
            return ParseResult(
                ParseStatus.SUCCESS,
                data=data,
                raw=text,
                strategy_used=ParseStrategy.JSON_LENIENT,
            )
        except Exception:
            pass

        # 2. Try code blocks
        blocks = _extract_code_blocks(text)
        for block in blocks:
            try:
                data = json.loads(block)
                return ParseResult(
                    ParseStatus.SUCCESS,
                    data=data,
                    raw=text,
                    strategy_used=ParseStrategy.MARKDOWN_CODE,
                    confidence=0.95,
                )
            except Exception:
                pass

        # 3. Extract embedded JSON objects
        objects = _extract_json_objects(text)
        if objects:
            data = objects[0] if len(objects) == 1 else objects
            return ParseResult(
                ParseStatus.PARTIAL,
                data=data,
                raw=text,
                strategy_used=ParseStrategy.JSON_LENIENT,
                confidence=0.8,
            )

        # 4. Fall through to text
        return ParseResult(
            ParseStatus.PARTIAL,
            data=text,
            raw=text,
            strategy_used=ParseStrategy.JSON_LENIENT,
            confidence=0.3,
            errors=["no JSON found; returning raw text"],
        )

    def _parse_repair(self, text: str) -> ParseResult:
        # First try lenient
        r = self._parse_lenient(text)
        if r.status == ParseStatus.SUCCESS:
            return r

        # Try repairing
        blocks = _extract_code_blocks(text) or [text]
        for block in blocks:
            repaired = _repair_json(block)
            try:
                data = json.loads(repaired)
                self._stats["repaired"] += 1
                return ParseResult(
                    ParseStatus.SUCCESS,
                    data=data,
                    raw=text,
                    strategy_used=ParseStrategy.JSON_REPAIR,
                    confidence=0.75,
                )
            except Exception:
                pass

        return ParseResult(
            ParseStatus.FAILED,
            raw=text,
            strategy_used=ParseStrategy.JSON_REPAIR,
            errors=["repair failed"],
        )

    def _parse_code_blocks(self, text: str) -> ParseResult:
        blocks = _extract_code_blocks(text)
        for block in blocks:
            try:
                data = json.loads(block)
                return ParseResult(
                    ParseStatus.SUCCESS,
                    data=data,
                    raw=text,
                    strategy_used=ParseStrategy.MARKDOWN_CODE,
                )
            except Exception:
                pass
        # Return raw block content
        if blocks:
            return ParseResult(
                ParseStatus.PARTIAL,
                data=blocks[0],
                raw=text,
                strategy_used=ParseStrategy.MARKDOWN_CODE,
                confidence=0.5,
            )
        return ParseResult(
            ParseStatus.FAILED,
            raw=text,
            strategy_used=ParseStrategy.MARKDOWN_CODE,
            errors=["no code blocks found"],
        )

    def parse_many(self, texts: list[str], **kwargs) -> list[ParseResult]:
        return [self.parse(t, **kwargs) for t in texts]

    def stats(self) -> dict:
        return {
            **self._stats,
            "success_rate": round(
                self._stats["success"] / max(self._stats["parsed"], 1), 4
            ),
        }


def build_jarvis_output_parser() -> OutputParser:
    return OutputParser(default_strategy=ParseStrategy.JSON_LENIENT)


def main():
    import sys

    parser = build_jarvis_output_parser()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Output parser demo...\n")

        cases = [
            ('{"status": "ok", "tokens": 150}', ParseStrategy.JSON_STRICT),
            (
                'Here is the result:\n```json\n{"model": "qwen3.5", "score": 0.95}\n```',
                ParseStrategy.MARKDOWN_CODE,
            ),
            ("The answer is: {'key': 'value', 'count': 42}", ParseStrategy.JSON_REPAIR),
            (
                "Model: qwen3.5\nStatus: healthy\nLatency: 120ms",
                ParseStrategy.KEY_VALUE,
            ),
            (
                "1. First item\n2. Second item\n- Third item\n* Fourth item",
                ParseStrategy.LIST,
            ),
            (
                '<think>reasoning here</think>\n{"answer": "42"}',
                ParseStrategy.JSON_LENIENT,
            ),
            ("score: (\\d+\\.?\\d*)", ParseStrategy.REGEX),  # bad regex demo
        ]

        for text, strategy in cases:
            r = parser.parse(text, strategy=strategy)
            icon = "✅" if r.ok else "❌"
            data_preview = str(r.data)[:60] if r.data else "—"
            print(
                f"  {icon} [{strategy.value:<15}] {r.status.value:<8} conf={r.confidence:.2f} → {data_preview}"
            )
            if r.errors:
                print(f"     errors: {r.errors}")

        print(f"\nStats: {json.dumps(parser.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(parser.stats(), indent=2))


if __name__ == "__main__":
    main()
