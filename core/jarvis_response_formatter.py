#!/usr/bin/env python3
"""
jarvis_response_formatter — Output formatting for LLM responses
Markdown, JSON, table, plain text, truncation, and citation injection
"""

import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.response_formatter")


class OutputFormat(str, Enum):
    PLAIN = "plain"
    MARKDOWN = "markdown"
    JSON = "json"
    TABLE = "table"
    COMPACT = "compact"  # single-line stripped
    BULLET = "bullet"  # auto bullet list
    CODE = "code"


class TruncateStrategy(str, Enum):
    END = "end"  # cut at end
    MIDDLE = "middle"  # cut middle, keep start+end
    SENTENCE = "sentence"  # cut at sentence boundary
    PARAGRAPH = "paragraph"


@dataclass
class FormatOptions:
    output_format: OutputFormat = OutputFormat.PLAIN
    max_length: int = 0  # 0 = no limit
    truncate_strategy: TruncateStrategy = TruncateStrategy.SENTENCE
    wrap_width: int = 0  # 0 = no wrap
    indent: int = 0
    citations: list[str] = field(default_factory=list)  # [source1, source2, ...]
    strip_think_tags: bool = True  # remove <think>...</think> blocks
    strip_markdown: bool = False  # remove markdown symbols for plain output
    code_language: str = "python"
    table_headers: list[str] = field(default_factory=list)
    json_indent: int = 2


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"\*(.+?)\*|_(.+?)_")
_MD_CODE_RE = re.compile(r"`(.+?)`")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def _strip_markdown(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_BULLET_RE.sub("• ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    return text.strip()


def _truncate_end(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _truncate_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    half = (max_len - 5) // 2
    return text[:half] + " ... " + text[-half:]


def _truncate_sentence(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    sentences = _SENT_SPLIT.split(text)
    result = ""
    for sent in sentences:
        candidate = (result + " " + sent).strip() if result else sent
        if len(candidate) > max_len:
            break
        result = candidate
    return (result + "...") if result else _truncate_end(text, max_len)


def _truncate_paragraph(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    paras = re.split(r"\n{2,}", text)
    result = ""
    for para in paras:
        candidate = (result + "\n\n" + para).strip() if result else para
        if len(candidate) > max_len:
            break
        result = candidate
    return (result + "\n\n...") if result else _truncate_end(text, max_len)


def _to_bullet(text: str) -> str:
    """Convert newline-separated items or sentences to bullet list."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) == 1:
        # Split by sentences
        lines = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    return "\n".join(f"• {line}" for line in lines)


def _rows_to_table(rows: list[list[str]], headers: list[str] | None = None) -> str:
    if not rows:
        return ""
    all_rows = ([headers] if headers else []) + rows
    widths = [max(len(str(cell)) for cell in col) for col in zip(*all_rows)]
    lines = []
    for i, row in enumerate(all_rows):
        line = " | ".join(str(cell).ljust(w) for cell, w in zip(row, widths))
        lines.append(line)
        if i == 0 and headers:
            lines.append("-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def _add_citations(text: str, citations: list[str]) -> str:
    if not citations:
        return text
    refs = "\n\n**Sources:**\n" + "\n".join(
        f"[{i + 1}] {c}" for i, c in enumerate(citations)
    )
    return text + refs


class ResponseFormatter:
    def __init__(self, default_options: FormatOptions | None = None):
        self._defaults = default_options or FormatOptions()
        self._stats: dict[str, int] = {"formatted": 0, "truncated": 0}

    def format(
        self,
        text: str,
        options: FormatOptions | None = None,
        data: Any = None,  # for JSON/TABLE formats
    ) -> str:
        opts = options or self._defaults
        self._stats["formatted"] += 1

        # 1. Strip think tags
        if opts.strip_think_tags:
            text = _strip_think(text)

        # 2. Strip markdown if needed
        if opts.strip_markdown:
            text = _strip_markdown(text)

        # 3. Format-specific transformations
        if opts.output_format == OutputFormat.JSON:
            if data is not None:
                text = json.dumps(data, indent=opts.json_indent, ensure_ascii=False)
            else:
                # Try to parse and re-format JSON in text
                try:
                    parsed = json.loads(text)
                    text = json.dumps(
                        parsed, indent=opts.json_indent, ensure_ascii=False
                    )
                except Exception:
                    pass

        elif opts.output_format == OutputFormat.TABLE:
            if data and isinstance(data, list) and data:
                if isinstance(data[0], dict):
                    headers = opts.table_headers or list(data[0].keys())
                    rows = [[str(row.get(h, "")) for h in headers] for row in data]
                    text = _rows_to_table(rows, headers)
                elif isinstance(data[0], (list, tuple)):
                    text = _rows_to_table(data, opts.table_headers or None)

        elif opts.output_format == OutputFormat.BULLET:
            text = _to_bullet(text)

        elif opts.output_format == OutputFormat.COMPACT:
            text = " ".join(text.split())

        elif opts.output_format == OutputFormat.CODE:
            text = f"```{opts.code_language}\n{text.strip()}\n```"

        # 4. Truncate
        if opts.max_length > 0 and len(text) > opts.max_length:
            self._stats["truncated"] += 1
            if opts.truncate_strategy == TruncateStrategy.END:
                text = _truncate_end(text, opts.max_length)
            elif opts.truncate_strategy == TruncateStrategy.MIDDLE:
                text = _truncate_middle(text, opts.max_length)
            elif opts.truncate_strategy == TruncateStrategy.SENTENCE:
                text = _truncate_sentence(text, opts.max_length)
            else:
                text = _truncate_paragraph(text, opts.max_length)

        # 5. Word wrap
        if opts.wrap_width > 0:
            text = textwrap.fill(text, width=opts.wrap_width, break_long_words=False)

        # 6. Indent
        if opts.indent > 0:
            pad = " " * opts.indent
            text = "\n".join(pad + line for line in text.splitlines())

        # 7. Citations
        if opts.citations:
            text = _add_citations(text, opts.citations)

        return text

    def plain(self, text: str, max_length: int = 0) -> str:
        return self.format(
            text,
            FormatOptions(
                output_format=OutputFormat.PLAIN,
                strip_markdown=True,
                max_length=max_length,
            ),
        )

    def compact(self, text: str) -> str:
        return self.format(
            text,
            FormatOptions(output_format=OutputFormat.COMPACT, strip_think_tags=True),
        )

    def as_json(self, data: Any) -> str:
        return self.format(
            "", FormatOptions(output_format=OutputFormat.JSON, json_indent=2), data=data
        )

    def as_table(self, data: list[dict], headers: list[str] | None = None) -> str:
        opts = FormatOptions(
            output_format=OutputFormat.TABLE, table_headers=headers or []
        )
        return self.format("", opts, data=data)

    def as_bullets(self, text: str) -> str:
        return self.format(text, FormatOptions(output_format=OutputFormat.BULLET))

    def stats(self) -> dict:
        return {**self._stats}


def build_jarvis_response_formatter() -> ResponseFormatter:
    return ResponseFormatter(
        default_options=FormatOptions(
            output_format=OutputFormat.PLAIN,
            strip_think_tags=True,
            max_length=0,
        )
    )


def main():
    import sys

    fmt = build_jarvis_response_formatter()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        raw = """<think>Let me think about this carefully...</think>

**JARVIS** is a multi-agent orchestration system. It manages:
- GPU clusters (M1, M2, OL1)
- LLM inference routing
- Trading pipelines

The system uses Redis for *state management* and pub/sub. Learn more at [docs](http://example.com).
"""
        print("=== Original ===")
        print(repr(raw[:100]))

        print("\n=== Plain (strip markdown) ===")
        print(fmt.plain(raw))

        print("\n=== Compact (single line) ===")
        print(fmt.compact(raw))

        print("\n=== Bullets ===")
        print(
            fmt.as_bullets(
                "GPU clusters. Redis pub/sub. Trading pipelines. LLM inference routing."
            )
        )

        print("\n=== Table ===")
        data = [
            {"node": "M1", "ip": "192.168.1.85", "status": "healthy"},
            {"node": "M2", "ip": "192.168.1.26", "status": "healthy"},
            {"node": "OL1", "ip": "127.0.0.1", "status": "healthy"},
        ]
        print(fmt.as_table(data))

        print("\n=== Truncated (100 chars, sentence boundary) ===")
        print(
            fmt.format(
                raw,
                FormatOptions(
                    max_length=100, strip_think_tags=True, strip_markdown=True
                ),
            )
        )

        print(f"\nStats: {json.dumps(fmt.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(fmt.stats(), indent=2))


if __name__ == "__main__":
    main()
