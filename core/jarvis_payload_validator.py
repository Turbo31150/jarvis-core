#!/usr/bin/env python3
"""
jarvis_payload_validator — Schema-based payload validation for LLM API requests
Validates structure, types, ranges, and required fields before dispatch
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.payload_validator")

REDIS_PREFIX = "jarvis:validate:"


class FieldType(str, Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    LIST = "list"
    DICT = "dict"
    ANY = "any"


@dataclass
class FieldSchema:
    name: str
    type: FieldType = FieldType.ANY
    required: bool = False
    nullable: bool = False
    min_val: float | None = None  # numeric min
    max_val: float | None = None  # numeric max
    min_len: int | None = None  # string/list min length
    max_len: int | None = None  # string/list max length
    allowed: list[Any] | None = None  # enum values
    pattern: str | None = None  # regex for strings
    default: Any = None


@dataclass
class Schema:
    name: str
    fields: list[FieldSchema] = field(default_factory=list)
    strict: bool = False  # reject unknown fields if True
    description: str = ""

    def field(self, name: str) -> FieldSchema | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass
class ValidationError:
    field: str
    message: str
    value: Any = None

    def to_dict(self) -> dict:
        return {"field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coerced: dict[str, Any] = field(default_factory=dict)  # field → new value
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": self.warnings,
            "coerced": list(self.coerced.keys()),
        }


def _coerce(value: Any, ftype: FieldType) -> tuple[Any, bool]:
    """Try to coerce value to target type. Returns (new_value, success)."""
    try:
        if ftype == FieldType.STRING:
            return str(value), True
        elif ftype == FieldType.INT:
            return int(value), True
        elif ftype == FieldType.FLOAT:
            return float(value), True
        elif ftype == FieldType.BOOL:
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes"), True
            return bool(value), True
    except (ValueError, TypeError):
        pass
    return value, False


class PayloadValidator:
    def __init__(self, coerce: bool = True):
        self._schemas: dict[str, Schema] = {}
        self._coerce = coerce
        self._stats: dict[str, int] = {
            "validated": 0,
            "passed": 0,
            "failed": 0,
            "coerced": 0,
        }
        self.redis: aioredis.Redis | None = None
        self._register_defaults()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _register_defaults(self):
        # OpenAI-compatible chat completion schema
        chat = Schema("chat_completion", strict=False)
        chat.fields = [
            FieldSchema(
                "model", FieldType.STRING, required=True, min_len=1, max_len=128
            ),
            FieldSchema(
                "messages", FieldType.LIST, required=True, min_len=1, max_len=200
            ),
            FieldSchema(
                "temperature", FieldType.FLOAT, min_val=0.0, max_val=2.0, default=1.0
            ),
            FieldSchema("max_tokens", FieldType.INT, min_val=1, max_val=128000),
            FieldSchema("top_p", FieldType.FLOAT, min_val=0.0, max_val=1.0),
            FieldSchema("stream", FieldType.BOOL, default=False),
            FieldSchema("stop", FieldType.ANY, nullable=True),
            FieldSchema("n", FieldType.INT, min_val=1, max_val=10, default=1),
        ]
        self.register(chat)

        # Embeddings schema
        embed = Schema("embeddings", strict=False)
        embed.fields = [
            FieldSchema("model", FieldType.STRING, required=True, min_len=1),
            FieldSchema("input", FieldType.ANY, required=True),
            FieldSchema(
                "encoding_format", FieldType.STRING, allowed=["float", "base64"]
            ),
        ]
        self.register(embed)

    def register(self, schema: Schema):
        self._schemas[schema.name] = schema

    def validate(self, payload: dict, schema_name: str) -> ValidationResult:
        t0 = time.time()
        schema = self._schemas.get(schema_name)
        if not schema:
            return ValidationResult(
                valid=False,
                errors=[ValidationError("_schema", f"Unknown schema: {schema_name}")],
            )

        self._stats["validated"] += 1
        errors: list[ValidationError] = []
        warnings: list[str] = []
        coerced: dict[str, Any] = {}
        data = dict(payload)  # work on a copy

        # Check strict mode
        if schema.strict:
            known = {f.name for f in schema.fields}
            for key in data:
                if key not in known:
                    warnings.append(f"Unknown field: {key}")

        for fs in schema.fields:
            val = data.get(fs.name)

            # Required check
            if val is None and fs.required:
                if fs.default is not None:
                    data[fs.name] = fs.default
                    coerced[fs.name] = fs.default
                else:
                    errors.append(ValidationError(fs.name, "Required field missing"))
                    continue

            if val is None:
                if fs.nullable or not fs.required:
                    if fs.default is not None and fs.name not in data:
                        data[fs.name] = fs.default
                    continue
                errors.append(ValidationError(fs.name, "Null not allowed"))
                continue

            # Type check + coerce
            if fs.type != FieldType.ANY:
                type_map = {
                    FieldType.STRING: str,
                    FieldType.INT: int,
                    FieldType.FLOAT: float,
                    FieldType.BOOL: bool,
                    FieldType.LIST: list,
                    FieldType.DICT: dict,
                }
                expected = type_map.get(fs.type)
                if expected and not isinstance(val, expected):
                    if self._coerce:
                        new_val, ok = _coerce(val, fs.type)
                        if ok:
                            data[fs.name] = new_val
                            coerced[fs.name] = new_val
                            val = new_val
                            self._stats["coerced"] += 1
                        else:
                            errors.append(
                                ValidationError(
                                    fs.name,
                                    f"Expected {fs.type.value}, got {type(val).__name__}",
                                )
                            )
                            continue
                    else:
                        errors.append(
                            ValidationError(
                                fs.name,
                                f"Expected {fs.type.value}, got {type(val).__name__}",
                            )
                        )
                        continue

            # Range checks
            if fs.min_val is not None and isinstance(val, (int, float)):
                if val < fs.min_val:
                    errors.append(
                        ValidationError(fs.name, f"Value {val} < min {fs.min_val}")
                    )
            if fs.max_val is not None and isinstance(val, (int, float)):
                if val > fs.max_val:
                    errors.append(
                        ValidationError(fs.name, f"Value {val} > max {fs.max_val}")
                    )

            # Length checks
            if fs.min_len is not None and hasattr(val, "__len__"):
                if len(val) < fs.min_len:
                    errors.append(
                        ValidationError(
                            fs.name, f"Length {len(val)} < min {fs.min_len}"
                        )
                    )
            if fs.max_len is not None and hasattr(val, "__len__"):
                if len(val) > fs.max_len:
                    errors.append(
                        ValidationError(
                            fs.name, f"Length {len(val)} > max {fs.max_len}"
                        )
                    )

            # Allowed values
            if fs.allowed and val not in fs.allowed:
                errors.append(
                    ValidationError(fs.name, f"Value {val!r} not in {fs.allowed}")
                )

            # Pattern
            if fs.pattern and isinstance(val, str):
                if not re.fullmatch(fs.pattern, val):
                    errors.append(
                        ValidationError(fs.name, f"Doesn't match pattern {fs.pattern}")
                    )

        valid = len(errors) == 0
        if valid:
            self._stats["passed"] += 1
        else:
            self._stats["failed"] += 1

        return ValidationResult(
            valid=valid,
            errors=errors,
            warnings=warnings,
            coerced=coerced,
            duration_ms=(time.time() - t0) * 1000,
        )

    def validate_chat(self, payload: dict) -> ValidationResult:
        return self.validate(payload, "chat_completion")

    def validate_embed(self, payload: dict) -> ValidationResult:
        return self.validate(payload, "embeddings")

    def schemas(self) -> list[str]:
        return list(self._schemas.keys())

    def stats(self) -> dict:
        return {**self._stats, "schemas": len(self._schemas)}


def build_jarvis_validator() -> PayloadValidator:
    return PayloadValidator(coerce=True)


async def main():
    import sys

    v = build_jarvis_validator()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        cases = [
            (
                "VALID chat",
                {
                    "model": "qwen3.5-9b",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "temperature": 0.7,
                },
                "chat_completion",
            ),
            (
                "MISSING model",
                {"messages": [{"role": "user", "content": "Hi"}]},
                "chat_completion",
            ),
            (
                "BAD temperature (coerced)",
                {
                    "model": "qwen",
                    "messages": [{"role": "user", "content": "x"}],
                    "temperature": "0.5",
                },
                "chat_completion",
            ),
            (
                "OUT OF RANGE temperature",
                {
                    "model": "qwen",
                    "messages": [{"role": "user", "content": "x"}],
                    "temperature": 5.0,
                },
                "chat_completion",
            ),
            (
                "VALID embed",
                {"model": "nomic-embed", "input": ["hello", "world"]},
                "embeddings",
            ),
        ]
        for label, payload, schema in cases:
            r = v.validate(payload, schema)
            status = "✅" if r.valid else "❌"
            errs = [e.message for e in r.errors]
            print(f"{status} {label}: errors={errs} coerced={list(r.coerced.keys())}")

        print(f"\nStats: {json.dumps(v.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(v.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
