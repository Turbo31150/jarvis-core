#!/usr/bin/env python3
"""
jarvis_response_filter — Post-processing filter chain for LLM outputs
Applies transformations: trim, truncate, strip think tags, format JSON, redact PII
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.response_filter")

REDIS_PREFIX = "jarvis:filter:"


class FilterOp(str, Enum):
    TRIM = "trim"  # strip whitespace
    TRUNCATE = "truncate"  # max character limit
    STRIP_THINK = "strip_think"  # remove <think>...</think> tags
    STRIP_MARKDOWN = "strip_markdown"  # remove markdown syntax
    EXTRACT_JSON = "extract_json"  # extract first JSON block
    EXTRACT_CODE = "extract_code"  # extract code from ```fences```
    REDACT_PII = "redact_pii"  # redact emails, phones, etc.
    NORMALIZE_NEWLINES = "normalize_newlines"
    LOWERCASE = "lowercase"
    REMOVE_CITATIONS = "remove_citations"  # [1], [2], etc.
    CUSTOM = "custom"


@dataclass
class FilterStep:
    op: FilterOp
    params: dict = field(default_factory=dict)
    name: str = ""

    def __post_init__(self):
        if not self.name:
            self.name = self.op.value


@dataclass
class FilterResult:
    original: str
    filtered: str
    steps_applied: list[str] = field(default_factory=list)
    modified: bool = False
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "modified": self.modified,
            "steps_applied": self.steps_applied,
            "original_len": len(self.original),
            "filtered_len": len(self.filtered),
            "duration_ms": round(self.duration_ms, 2),
        }


# ---- Filter implementations ----

_THINK_PATTERN = re.compile(r"<think>[\s\S]*?</think>", re.I)
_THINK_OPEN = re.compile(r"<think>[\s\S]*$", re.I)  # unclosed think tag
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"\*(.+?)\*")
_MD_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_CODE_INLINE = re.compile(r"`([^`]+)`")
_CODE_FENCE = re.compile(r"```(?:\w+)?\n?([\s\S]+?)```", re.MULTILINE)
_JSON_BLOCK = re.compile(
    r"```(?:json)?\s*(\{[\s\S]+?\})\s*```|(\{[\s\S]+\})", re.MULTILINE
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"\b(\+\d{1,3}[\s\-])?\(?\d{3}\)?[\s\-]\d{3}[\s\-]\d{4}\b")
_CITATION = re.compile(r"\[\d+\]")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def _apply_op(text: str, step: FilterStep, custom_fns: dict[str, Callable]) -> str:
    op = step.op
    p = step.params

    if op == FilterOp.TRIM:
        return text.strip()

    elif op == FilterOp.TRUNCATE:
        max_chars = p.get("max_chars", 4000)
        suffix = p.get("suffix", "...")
        if len(text) > max_chars:
            return text[: max_chars - len(suffix)] + suffix
        return text

    elif op == FilterOp.STRIP_THINK:
        text = _THINK_PATTERN.sub("", text)
        text = _THINK_OPEN.sub("", text)
        return text.strip()

    elif op == FilterOp.STRIP_MARKDOWN:
        text = _MD_BOLD.sub(r"\1", text)
        text = _MD_ITALIC.sub(r"\1", text)
        text = _MD_HEADER.sub("", text)
        text = _MD_LINK.sub(r"\1", text)
        text = _MD_CODE_INLINE.sub(r"\1", text)
        text = _CODE_FENCE.sub(r"\1", text)
        return text.strip()

    elif op == FilterOp.EXTRACT_JSON:
        # Try fenced JSON first, then bare JSON object
        m = _JSON_BLOCK.search(text)
        if m:
            candidate = m.group(1) or m.group(2)
            if candidate:
                try:
                    parsed = json.loads(candidate)
                    return json.dumps(parsed, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
        return text

    elif op == FilterOp.EXTRACT_CODE:
        m = _CODE_FENCE.search(text)
        return m.group(1).strip() if m else text

    elif op == FilterOp.REDACT_PII:
        text = _EMAIL.sub("[EMAIL]", text)
        text = _PHONE.sub("[PHONE]", text)
        return text

    elif op == FilterOp.NORMALIZE_NEWLINES:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _MULTI_NEWLINE.sub("\n\n", text)
        return text.strip()

    elif op == FilterOp.LOWERCASE:
        return text.lower()

    elif op == FilterOp.REMOVE_CITATIONS:
        return _CITATION.sub("", text).strip()

    elif op == FilterOp.CUSTOM:
        fn_name = p.get("fn", "")
        fn = custom_fns.get(fn_name)
        if fn:
            return fn(text, p)

    return text


class ResponseFilter:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._pipelines: dict[str, list[FilterStep]] = {}
        self._custom_fns: dict[str, Callable] = {}
        self._stats: dict[str, int] = {
            "filtered": 0,
            "modified": 0,
            "pipeline_runs": 0,
        }
        self._setup_defaults()

    def _setup_defaults(self):
        self._pipelines["standard"] = [
            FilterStep(FilterOp.STRIP_THINK),
            FilterStep(FilterOp.TRIM),
            FilterStep(FilterOp.NORMALIZE_NEWLINES),
        ]
        self._pipelines["clean_text"] = [
            FilterStep(FilterOp.STRIP_THINK),
            FilterStep(FilterOp.STRIP_MARKDOWN),
            FilterStep(FilterOp.NORMALIZE_NEWLINES),
            FilterStep(FilterOp.TRIM),
        ]
        self._pipelines["extract_json"] = [
            FilterStep(FilterOp.STRIP_THINK),
            FilterStep(FilterOp.EXTRACT_JSON),
        ]
        self._pipelines["safe_output"] = [
            FilterStep(FilterOp.STRIP_THINK),
            FilterStep(FilterOp.REDACT_PII),
            FilterStep(FilterOp.TRIM),
            FilterStep(FilterOp.TRUNCATE, {"max_chars": 8000}),
        ]

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def register_pipeline(self, name: str, steps: list[FilterStep]):
        self._pipelines[name] = steps

    def register_custom(self, name: str, fn: Callable):
        self._custom_fns[name] = fn

    def apply(self, text: str, steps: list[FilterStep]) -> FilterResult:
        t0 = time.time()
        self._stats["filtered"] += 1
        current = text
        applied = []

        for step in steps:
            before = current
            current = _apply_op(current, step, self._custom_fns)
            if current != before:
                applied.append(step.name)

        modified = current != text
        if modified:
            self._stats["modified"] += 1

        return FilterResult(
            original=text,
            filtered=current,
            steps_applied=applied,
            modified=modified,
            duration_ms=(time.time() - t0) * 1000,
        )

    def run_pipeline(self, text: str, pipeline: str = "standard") -> FilterResult:
        self._stats["pipeline_runs"] += 1
        steps = self._pipelines.get(pipeline, self._pipelines["standard"])
        return self.apply(text, steps)

    def filter_messages(
        self, messages: list[dict], pipeline: str = "standard"
    ) -> list[dict]:
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and msg.get("role") == "assistant":
                fr = self.run_pipeline(content, pipeline)
                msg = dict(msg)
                msg["content"] = fr.filtered
            result.append(msg)
        return result

    def pipelines(self) -> list[str]:
        return list(self._pipelines.keys())

    def stats(self) -> dict:
        return {**self._stats, "pipelines": len(self._pipelines)}


def build_jarvis_response_filter() -> ResponseFilter:
    rf = ResponseFilter()
    # Add cluster-specific pipeline
    rf.register_pipeline(
        "local_model",
        [
            FilterStep(FilterOp.STRIP_THINK),
            FilterStep(FilterOp.REMOVE_CITATIONS),
            FilterStep(FilterOp.NORMALIZE_NEWLINES),
            FilterStep(FilterOp.TRUNCATE, {"max_chars": 16000}),
            FilterStep(FilterOp.TRIM),
        ],
    )
    return rf


async def main():
    import sys

    rf = build_jarvis_response_filter()
    await rf.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        samples = [
            (
                "standard",
                "<think>Let me analyze this carefully...\nOkay so the answer is 42.</think>\n\nThe answer is **42**.\n\n\n\nIt was confirmed by research.",
            ),
            (
                "extract_json",
                'Here is the result:\n```json\n{"status": "ok", "value": 42}\n```\nHope that helps!',
            ),
            (
                "safe_output",
                "Contact john@example.com or call 555-123-4567 for more info. The API key is abc123.",
            ),
            (
                "clean_text",
                "## Summary\n\nThe **JARVIS** system uses *adaptive* routing [1][2].\n\n`code` example here.",
            ),
        ]

        for pipeline, text in samples:
            result = rf.run_pipeline(text, pipeline)
            print(f"Pipeline: {pipeline}")
            print(f"  Input:    {text[:80]!r}")
            print(f"  Output:   {result.filtered[:80]!r}")
            print(f"  Modified: {result.modified}  Steps: {result.steps_applied}")
            print()

        print(f"Stats: {json.dumps(rf.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(rf.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
