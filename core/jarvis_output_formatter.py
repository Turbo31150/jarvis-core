#!/usr/bin/env python3
"""
jarvis_output_formatter — Structured output formatting and validation
Ensures LLM responses conform to expected schemas (JSON, markdown, code, etc.)
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jarvis.output_formatter")


@dataclass
class FormatResult:
    raw: str
    parsed: Any
    format_type: str
    valid: bool
    errors: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "format_type": self.format_type,
            "valid": self.valid,
            "errors": self.errors,
            "parsed": self.parsed,
        }


class OutputFormatter:
    """Parse and validate LLM output in various formats."""

    # ── JSON extraction ───────────────────────────────────────────────────────

    @staticmethod
    def extract_json(text: str) -> tuple[Any, list[str]]:
        """Extract first valid JSON object or array from text."""
        errors = []

        # Try direct parse
        try:
            return json.loads(text.strip()), []
        except json.JSONDecodeError:
            pass

        # Try code block
        block_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if block_match:
            try:
                return json.loads(block_match.group(1).strip()), []
            except json.JSONDecodeError as e:
                errors.append(f"Code block JSON error: {e}")

        # Try first {...} or [...]
        for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
            match = re.search(pattern, text)
            if match:
                try:
                    return json.loads(match.group(0)), []
                except json.JSONDecodeError as e:
                    errors.append(f"Inline JSON error: {e}")

        errors.append("No valid JSON found")
        return None, errors

    @staticmethod
    def extract_code(text: str, language: str = "") -> tuple[str, str]:
        """Extract code from markdown code block."""
        pattern = rf"```{language}?\s*([\s\S]*?)```"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip(), language
        # Fallback: return as-is
        return text.strip(), "unknown"

    @staticmethod
    def extract_list(text: str) -> list[str]:
        """Extract bullet/numbered list items."""
        items = []
        for line in text.split("\n"):
            line = line.strip()
            # Match: "- item", "* item", "1. item", "1) item"
            m = re.match(r"^[-*•]|\d+[.)]\s+(.+)", line)
            if m:
                content = re.sub(r"^[-*•\d.)\s]+", "", line).strip()
                if content:
                    items.append(content)
        return items

    @staticmethod
    def extract_key_value(text: str) -> dict[str, str]:
        """Extract key: value pairs from text."""
        result = {}
        for line in text.split("\n"):
            m = re.match(r"^[*-]?\s*\*{0,2}([\w\s]+?)\*{0,2}:\s*(.+)$", line.strip())
            if m:
                key = m.group(1).strip().lower().replace(" ", "_")
                value = m.group(2).strip()
                result[key] = value
        return result

    # ── Validation ────────────────────────────────────────────────────────────

    @staticmethod
    def validate_json_schema(data: Any, schema: dict) -> list[str]:
        """Basic JSON schema validation (no external deps)."""
        errors = []

        def check(d, s, path=""):
            t = s.get("type")
            if t == "object":
                if not isinstance(d, dict):
                    errors.append(f"{path}: expected object, got {type(d).__name__}")
                    return
                for req in s.get("required", []):
                    if req not in d:
                        errors.append(f"{path}.{req}: required field missing")
                for k, v in s.get("properties", {}).items():
                    if k in d:
                        check(d[k], v, f"{path}.{k}")
            elif t == "array":
                if not isinstance(d, list):
                    errors.append(f"{path}: expected array")
                    return
                item_schema = s.get("items", {})
                for i, item in enumerate(d):
                    check(item, item_schema, f"{path}[{i}]")
            elif t == "string" and not isinstance(d, str):
                errors.append(f"{path}: expected string, got {type(d).__name__}")
            elif t == "integer" and not isinstance(d, int):
                errors.append(f"{path}: expected integer")
            elif t == "number" and not isinstance(d, (int, float)):
                errors.append(f"{path}: expected number")
            elif t == "boolean" and not isinstance(d, bool):
                errors.append(f"{path}: expected boolean")

            # Enum check
            if "enum" in s and d not in s["enum"]:
                errors.append(f"{path}: value {d!r} not in enum {s['enum']}")

        check(data, schema)
        return errors

    # ── High-level parse ─────────────────────────────────────────────────────

    def parse(
        self,
        text: str,
        format_type: str = "auto",
        schema: dict | None = None,
    ) -> FormatResult:
        t0 = time.time()
        errors: list[str] = []
        parsed: Any = None

        if format_type == "auto":
            # Detect format
            if (
                "```" in text
                or text.strip().startswith("{")
                or text.strip().startswith("[")
            ):
                format_type = "json"
            elif re.search(r"^\d+[.)]\s|^[-*•]\s", text, re.MULTILINE):
                format_type = "list"
            else:
                format_type = "text"

        if format_type == "json":
            parsed, errors = self.extract_json(text)
            if parsed is not None and schema:
                schema_errors = self.validate_json_schema(parsed, schema)
                errors.extend(schema_errors)

        elif format_type == "code":
            code, lang = self.extract_code(text)
            parsed = {"code": code, "language": lang}

        elif format_type == "list":
            parsed = self.extract_list(text)
            if not parsed:
                errors.append("No list items found")

        elif format_type == "kv":
            parsed = self.extract_key_value(text)

        elif format_type == "text":
            parsed = text.strip()

        else:
            parsed = text.strip()

        return FormatResult(
            raw=text,
            parsed=parsed,
            format_type=format_type,
            valid=len(errors) == 0 and parsed is not None,
            errors=errors,
            latency_ms=round((time.time() - t0) * 1000, 2),
        )

    def ensure_json(
        self,
        text: str,
        schema: dict | None = None,
        fallback: Any = None,
    ) -> Any:
        """Return parsed JSON or fallback value."""
        result = self.parse(text, "json", schema)
        if result.valid:
            return result.parsed
        if fallback is not None:
            return fallback
        raise ValueError(f"Could not parse JSON: {result.errors}")

    def ensure_list(self, text: str) -> list[str]:
        result = self.parse(text, "list")
        if result.valid and isinstance(result.parsed, list):
            return result.parsed
        # Fallback: split by newlines
        return [line.strip() for line in text.split("\n") if line.strip()]

    def clean_text(self, text: str) -> str:
        """Remove markdown, code blocks, excessive whitespace."""
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(r"`[^`]+`", lambda m: m.group(0)[1:-1], text)
        text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main():
    import sys

    formatter = OutputFormatter()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        samples = [
            ("json", '{"name": "JARVIS", "version": 3, "active": true}'),
            ("list", "- Redis\n- Python\n- CUDA\n- LLM"),
            ("kv", "Model: qwen3.5-9b\nBackend: M1\nTokens: 1024"),
            ("code", "```python\ndef hello():\n    return 'world'\n```"),
            ("auto", "Here is the answer: 42"),
        ]
        for fmt, text in samples:
            result = formatter.parse(text, fmt)
            status = "✅" if result.valid else "❌"
            print(f"{status} [{fmt}] → {str(result.parsed)[:80]}")

    elif cmd == "parse" and len(sys.argv) > 3:
        fmt = sys.argv[2]
        text = sys.argv[3]
        result = formatter.parse(text, fmt)
        print(json.dumps(result.to_dict(), indent=2))

    elif cmd == "clean" and len(sys.argv) > 2:
        print(formatter.clean_text(sys.argv[2]))


if __name__ == "__main__":
    main()
