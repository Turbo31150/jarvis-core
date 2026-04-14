#!/usr/bin/env python3
"""
jarvis_schema_validator — Lightweight JSON schema validation without external deps
Validates LLM outputs, API payloads, and config files against type schemas
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jarvis.schema_validator")


@dataclass
class ValidationError:
    path: str
    message: str
    value: Any = None


@dataclass
class ValidationResult:
    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "error_count": len(self.errors),
            "errors": [{"path": e.path, "message": e.message} for e in self.errors],
            "warnings": self.warnings,
            "latency_ms": round(self.latency_ms, 1),
        }


# Type mapping
TYPE_MAP = {
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "number": (int, float),
    "float": float,
    "boolean": bool,
    "bool": bool,
    "array": list,
    "list": list,
    "object": dict,
    "dict": dict,
    "null": type(None),
    "none": type(None),
    "any": object,
}


class SchemaValidator:
    def __init__(self):
        self._schemas: dict[str, dict] = {}
        self._stats: dict[str, int] = {"valid": 0, "invalid": 0, "total": 0}

    def register(self, name: str, schema: dict):
        self._schemas[name] = schema

    def _validate_type(
        self, value: Any, type_spec: Any, path: str
    ) -> list[ValidationError]:
        if type_spec == "any":
            return []
        expected = TYPE_MAP.get(type_spec) if isinstance(type_spec, str) else type_spec
        if expected is None:
            return []
        if not isinstance(value, expected):
            return [
                ValidationError(
                    path=path,
                    message=f"Expected {type_spec}, got {type(value).__name__}",
                    value=value,
                )
            ]
        return []

    def _validate_value(
        self, value: Any, schema: dict, path: str
    ) -> tuple[list[ValidationError], list[str]]:
        errors: list[ValidationError] = []
        warnings: list[str] = []

        # Type check
        if "type" in schema:
            errors.extend(self._validate_type(value, schema["type"], path))
            if errors:
                return errors, warnings  # Stop further checks if wrong type

        # String constraints
        if isinstance(value, str):
            if "min_length" in schema and len(value) < schema["min_length"]:
                errors.append(
                    ValidationError(
                        path, f"String too short: {len(value)} < {schema['min_length']}"
                    )
                )
            if "max_length" in schema and len(value) > schema["max_length"]:
                errors.append(
                    ValidationError(
                        path, f"String too long: {len(value)} > {schema['max_length']}"
                    )
                )
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append(
                    ValidationError(
                        path, f"Doesn't match pattern: {schema['pattern']}", value
                    )
                )
            if "enum" in schema and value not in schema["enum"]:
                errors.append(
                    ValidationError(path, f"Not in enum {schema['enum']}", value)
                )

        # Numeric constraints
        if isinstance(value, (int, float)):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(
                    ValidationError(
                        path, f"Value {value} < minimum {schema['minimum']}"
                    )
                )
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(
                    ValidationError(
                        path, f"Value {value} > maximum {schema['maximum']}"
                    )
                )
            if "exclusive_minimum" in schema and value <= schema["exclusive_minimum"]:
                errors.append(
                    ValidationError(
                        path, f"Value {value} must be > {schema['exclusive_minimum']}"
                    )
                )
            if "multiple_of" in schema and value % schema["multiple_of"] != 0:
                errors.append(
                    ValidationError(
                        path, f"Value {value} not multiple of {schema['multiple_of']}"
                    )
                )

        # Array constraints
        if isinstance(value, list):
            if "min_items" in schema and len(value) < schema["min_items"]:
                errors.append(
                    ValidationError(
                        path, f"Array too short: {len(value)} < {schema['min_items']}"
                    )
                )
            if "max_items" in schema and len(value) > schema["max_items"]:
                errors.append(
                    ValidationError(
                        path, f"Array too long: {len(value)} > {schema['max_items']}"
                    )
                )
            if "items" in schema:
                for i, item in enumerate(value):
                    sub_errs, sub_warns = self._validate_value(
                        item, schema["items"], f"{path}[{i}]"
                    )
                    errors.extend(sub_errs)
                    warnings.extend(sub_warns)
            if "unique_items" in schema and schema["unique_items"]:
                seen = []
                for item in value:
                    key = json.dumps(item, sort_keys=True, default=str)
                    if key in seen:
                        errors.append(ValidationError(path, f"Duplicate item: {item}"))
                    seen.append(key)

        # Object/dict constraints
        if isinstance(value, dict):
            required = schema.get("required", [])
            for req_key in required:
                if req_key not in value:
                    errors.append(
                        ValidationError(f"{path}.{req_key}", "Required field missing")
                    )

            properties = schema.get("properties", {})
            for prop_name, prop_schema in properties.items():
                if prop_name in value:
                    sub_errs, sub_warns = self._validate_value(
                        value[prop_name], prop_schema, f"{path}.{prop_name}"
                    )
                    errors.extend(sub_errs)
                    warnings.extend(sub_warns)
                elif prop_schema.get("default") is not None:
                    warnings.append(
                        f"{path}.{prop_name}: using default={prop_schema['default']}"
                    )

            additional = schema.get("additional_properties", True)
            if not additional:
                for key in value:
                    if key not in properties:
                        errors.append(
                            ValidationError(
                                f"{path}.{key}", "Additional property not allowed"
                            )
                        )

            if "min_properties" in schema and len(value) < schema["min_properties"]:
                errors.append(
                    ValidationError(
                        path,
                        f"Too few properties: {len(value)} < {schema['min_properties']}",
                    )
                )
            if "max_properties" in schema and len(value) > schema["max_properties"]:
                errors.append(
                    ValidationError(
                        path,
                        f"Too many properties: {len(value)} > {schema['max_properties']}",
                    )
                )

        # Conditional: if/then/else
        if "if" in schema:
            cond_result, _ = self._validate_value(value, schema["if"], path)
            branch = schema.get("then" if not cond_result else "else", {})
            if branch:
                sub_errs, sub_warns = self._validate_value(value, branch, path)
                errors.extend(sub_errs)
                warnings.extend(sub_warns)

        # anyOf / oneOf / allOf
        if "any_of" in schema:
            if all(self._validate_value(value, s, path)[0] for s in schema["any_of"]):
                errors.append(
                    ValidationError(path, "Doesn't match any of the allowed schemas")
                )

        return errors, warnings

    def validate(self, data: Any, schema: dict | str) -> ValidationResult:
        t0 = time.time()
        if isinstance(schema, str):
            schema = self._schemas.get(schema, {})

        errors, warnings = self._validate_value(data, schema, "$")
        valid = len(errors) == 0
        self._stats["total"] += 1
        self._stats["valid" if valid else "invalid"] += 1

        return ValidationResult(
            valid=valid,
            errors=errors,
            warnings=warnings,
            latency_ms=(time.time() - t0) * 1000,
        )

    def validate_llm_output(self, text: str, expected_schema: dict) -> ValidationResult:
        """Try to parse text as JSON and validate against schema."""
        try:
            # Extract JSON from text (may be wrapped in markdown)
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                data = json.loads(m.group(0))
            else:
                data = json.loads(text)
            return self.validate(data, expected_schema)
        except json.JSONDecodeError as e:
            return ValidationResult(
                valid=False,
                errors=[ValidationError("$", f"Invalid JSON: {e}", text[:100])],
            )

    def stats(self) -> dict:
        return {
            **self._stats,
            "valid_rate": round(self._stats["valid"] / max(self._stats["total"], 1), 3),
            "registered_schemas": list(self._schemas.keys()),
        }


# Pre-built schemas for JARVIS
JARVIS_SCHEMAS = {
    "infer_request": {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "min_length": 1, "max_length": 32768},
            "model": {"type": "string"},
            "temperature": {"type": "number", "minimum": 0.0, "maximum": 2.0},
            "max_tokens": {"type": "integer", "minimum": 1, "maximum": 65536},
            "stream": {"type": "boolean"},
        },
    },
    "model_record": {
        "type": "object",
        "required": ["model_id", "node"],
        "properties": {
            "model_id": {"type": "string", "min_length": 1},
            "node": {"type": "string", "enum": ["M1", "M2", "M3", "OL1"]},
            "tier": {
                "type": "string",
                "enum": ["fast", "standard", "premium", "specialist", "utility"],
            },
            "context_length": {"type": "integer", "minimum": 512},
        },
    },
    "chat_message": {
        "type": "object",
        "required": ["role", "content"],
        "properties": {
            "role": {"type": "string", "enum": ["system", "user", "assistant", "tool"]},
            "content": {"type": "string", "min_length": 1},
        },
        "additional_properties": False,
    },
}


async def main():
    import sys

    validator = SchemaValidator()
    for name, schema in JARVIS_SCHEMAS.items():
        validator.register(name, schema)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        tests = [
            (
                "valid infer",
                {"prompt": "What is Redis?", "temperature": 0.7, "max_tokens": 512},
                "infer_request",
            ),
            (
                "missing prompt",
                {"model": "qwen3.5", "temperature": 0.7},
                "infer_request",
            ),
            (
                "bad temperature",
                {"prompt": "test", "temperature": 3.5},
                "infer_request",
            ),
            ("valid message", {"role": "user", "content": "Hello"}, "chat_message"),
            (
                "extra field",
                {"role": "user", "content": "Hi", "extra": "bad"},
                "chat_message",
            ),
            ("bad role", {"role": "admin", "content": "Hi"}, "chat_message"),
        ]
        for name, data, schema_name in tests:
            result = validator.validate(data, schema_name)
            status = "✅" if result.valid else "❌"
            errs = [e.message for e in result.errors]
            print(f"{status} {name}: {errs if errs else 'OK'}")

    elif cmd == "stats":
        print(json.dumps(validator.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
