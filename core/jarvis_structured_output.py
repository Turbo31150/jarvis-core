#!/usr/bin/env python3
"""
jarvis_structured_output — Structured output enforcement for LLM responses
Pydantic-free schema validation, type coercion, default injection, strict mode
"""

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("jarvis.structured_output")


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"
    ENUM = "enum"
    OPTIONAL_STRING = "optional_string"


class ValidationMode(str, Enum):
    STRICT = "strict"  # fail if schema mismatch
    COERCE = "coerce"  # try to convert types
    LENIENT = "lenient"  # fill defaults on mismatch


@dataclass
class FieldSchema:
    name: str
    type: FieldType
    required: bool = True
    default: Any = None
    description: str = ""
    enum_values: list[Any] = field(default_factory=list)
    min_length: int = 0
    max_length: int = 0
    min_value: float | None = None
    max_value: float | None = None
    nested: list["FieldSchema"] | None = None  # for DICT fields


@dataclass
class OutputSchema:
    name: str
    fields: list[FieldSchema]
    description: str = ""
    examples: list[dict] = field(default_factory=list)

    def field_map(self) -> dict[str, FieldSchema]:
        return {f.name: f for f in self.fields}

    def to_prompt_hint(self) -> str:
        lines = [f"Output format: {self.name}", "Required fields:"]
        for f in self.fields:
            req = "required" if f.required else f"optional (default: {f.default!r})"
            enum_hint = f" (one of: {f.enum_values})" if f.enum_values else ""
            lines.append(
                f"  - {f.name} ({f.type.value}){enum_hint}: {f.description} [{req}]"
            )
        if self.examples:
            lines.append("\nExample:")
            lines.append(json.dumps(self.examples[0], indent=2))
        return "\n".join(lines)


@dataclass
class ValidationResult:
    valid: bool
    data: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coerced: list[str] = field(default_factory=list)
    defaults_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "data": self.data,
            "errors": self.errors,
            "warnings": self.warnings,
            "coerced": self.coerced,
            "defaults_applied": self.defaults_applied,
        }


def _coerce_value(value: Any, field: FieldSchema) -> tuple[Any, bool]:
    """Returns (coerced_value, success)."""
    try:
        ft = field.type
        if ft == FieldType.STRING or ft == FieldType.OPTIONAL_STRING:
            return str(value) if value is not None else "", True
        elif ft == FieldType.INTEGER:
            if isinstance(value, str):
                value = re.sub(r"[^\d\-]", "", value)
            return int(float(value)), True
        elif ft == FieldType.FLOAT:
            if isinstance(value, str):
                value = re.sub(r"[^\d\-\.]", "", value)
            return float(value), True
        elif ft == FieldType.BOOLEAN:
            if isinstance(value, bool):
                return value, True
            return str(value).lower() in ("true", "1", "yes", "on"), True
        elif ft == FieldType.LIST:
            if isinstance(value, str):
                # Try JSON parse
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return parsed, True
                except Exception:
                    pass
                # Split by comma
                return [v.strip() for v in value.split(",") if v.strip()], True
            return list(value), True
        elif ft == FieldType.DICT:
            if isinstance(value, str):
                return json.loads(value), True
            return dict(value), True
        elif ft == FieldType.ENUM:
            if value in field.enum_values:
                return value, True
            # Try case-insensitive
            for ev in field.enum_values:
                if str(ev).lower() == str(value).lower():
                    return ev, True
            return value, False
    except Exception:
        pass
    return value, False


class StructuredOutput:
    def __init__(self, mode: ValidationMode = ValidationMode.COERCE):
        self._schemas: dict[str, OutputSchema] = {}
        self._mode = mode
        self._stats: dict[str, int] = {
            "validated": 0,
            "valid": 0,
            "invalid": 0,
            "coerced": 0,
        }

    def register(self, schema: OutputSchema):
        self._schemas[schema.name] = schema

    def validate(
        self,
        data: dict | str | None,
        schema_name: str,
        mode: ValidationMode | None = None,
    ) -> ValidationResult:
        self._stats["validated"] += 1
        mode = mode or self._mode
        schema = self._schemas.get(schema_name)
        if not schema:
            self._stats["invalid"] += 1
            return ValidationResult(
                False, {}, errors=[f"unknown schema: {schema_name!r}"]
            )

        # Parse string input
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                # Try to extract JSON from text
                m = re.search(r"\{.*\}", data, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except Exception:
                        self._stats["invalid"] += 1
                        return ValidationResult(
                            False, {}, errors=["could not parse input as JSON"]
                        )
                else:
                    self._stats["invalid"] += 1
                    return ValidationResult(False, {}, errors=["input is not JSON"])

        if data is None:
            data = {}

        result_data: dict[str, Any] = {}
        errors: list[str] = []
        warnings: list[str] = []
        coerced: list[str] = []
        defaults_applied: list[str] = []

        field_map = schema.field_map()

        # Check for unexpected keys
        for key in data:
            if key not in field_map:
                warnings.append(f"unexpected field: {key!r}")

        for fs in schema.fields:
            raw_value = data.get(fs.name)

            # Missing field
            if raw_value is None:
                if fs.required and fs.default is None:
                    if mode == ValidationMode.STRICT:
                        errors.append(f"missing required field: {fs.name!r}")
                        continue
                    else:
                        warnings.append(f"missing required field: {fs.name!r}")
                # Apply default
                if fs.default is not None:
                    result_data[fs.name] = fs.default
                    defaults_applied.append(fs.name)
                elif fs.type == FieldType.OPTIONAL_STRING:
                    result_data[fs.name] = None
                elif fs.type == FieldType.LIST:
                    result_data[fs.name] = []
                elif fs.type == FieldType.DICT:
                    result_data[fs.name] = {}
                continue

            # Type check
            expected_python = {
                FieldType.STRING: str,
                FieldType.OPTIONAL_STRING: (str, type(None)),
                FieldType.INTEGER: (int, float),
                FieldType.FLOAT: (int, float),
                FieldType.BOOLEAN: bool,
                FieldType.LIST: list,
                FieldType.DICT: dict,
            }.get(fs.type)

            type_ok = expected_python is None or isinstance(raw_value, expected_python)

            if not type_ok:
                if mode == ValidationMode.STRICT:
                    errors.append(
                        f"type error {fs.name!r}: expected {fs.type.value}, got {type(raw_value).__name__}"
                    )
                    continue
                elif mode in (ValidationMode.COERCE, ValidationMode.LENIENT):
                    coerced_val, success = _coerce_value(raw_value, fs)
                    if success:
                        raw_value = coerced_val
                        coerced.append(fs.name)
                        self._stats["coerced"] += 1
                    else:
                        if mode == ValidationMode.STRICT:
                            errors.append(f"coercion failed for {fs.name!r}")
                            continue
                        else:
                            warnings.append(
                                f"coercion failed for {fs.name!r}, using raw value"
                            )

            # Enum validation
            if fs.type == FieldType.ENUM and fs.enum_values:
                if raw_value not in fs.enum_values:
                    errors.append(
                        f"invalid enum value for {fs.name!r}: {raw_value!r} not in {fs.enum_values}"
                    )
                    if mode != ValidationMode.LENIENT:
                        continue

            # Length/range constraints
            if isinstance(raw_value, str):
                if fs.min_length > 0 and len(raw_value) < fs.min_length:
                    errors.append(f"{fs.name!r}: too short (min {fs.min_length})")
                if fs.max_length > 0 and len(raw_value) > fs.max_length:
                    raw_value = raw_value[: fs.max_length]
                    warnings.append(f"{fs.name!r}: truncated to {fs.max_length}")
            if isinstance(raw_value, (int, float)):
                if fs.min_value is not None and raw_value < fs.min_value:
                    errors.append(f"{fs.name!r}: {raw_value} < min {fs.min_value}")
                if fs.max_value is not None and raw_value > fs.max_value:
                    errors.append(f"{fs.name!r}: {raw_value} > max {fs.max_value}")

            result_data[fs.name] = raw_value

        valid = len(errors) == 0
        if valid:
            self._stats["valid"] += 1
        else:
            self._stats["invalid"] += 1

        return ValidationResult(
            valid, result_data, errors, warnings, coerced, defaults_applied
        )

    def prompt_hint(self, schema_name: str) -> str:
        schema = self._schemas.get(schema_name)
        return schema.to_prompt_hint() if schema else f"Unknown schema: {schema_name}"

    def stats(self) -> dict:
        return {
            **self._stats,
            "valid_rate": round(
                self._stats["valid"] / max(self._stats["validated"], 1), 4
            ),
            "schemas": len(self._schemas),
        }


def build_jarvis_structured_output() -> StructuredOutput:
    so = StructuredOutput(mode=ValidationMode.COERCE)

    so.register(
        OutputSchema(
            name="inference_response",
            description="Standard LLM inference response",
            fields=[
                FieldSchema("model", FieldType.STRING, description="Model name"),
                FieldSchema("response", FieldType.STRING, description="Generated text"),
                FieldSchema("tokens_used", FieldType.INTEGER, default=0),
                FieldSchema("latency_ms", FieldType.FLOAT, default=0.0),
                FieldSchema(
                    "confidence",
                    FieldType.FLOAT,
                    default=0.0,
                    min_value=0.0,
                    max_value=1.0,
                ),
                FieldSchema(
                    "finish_reason",
                    FieldType.ENUM,
                    default="stop",
                    enum_values=["stop", "length", "error", "timeout"],
                ),
                FieldSchema("cached", FieldType.BOOLEAN, default=False),
            ],
            examples=[
                {
                    "model": "qwen3.5-9b",
                    "response": "Hello!",
                    "tokens_used": 5,
                    "finish_reason": "stop",
                }
            ],
        )
    )

    so.register(
        OutputSchema(
            name="trading_signal",
            description="Trading signal output",
            fields=[
                FieldSchema("symbol", FieldType.STRING, description="Trading pair"),
                FieldSchema(
                    "action", FieldType.ENUM, enum_values=["buy", "sell", "hold"]
                ),
                FieldSchema(
                    "confidence", FieldType.FLOAT, min_value=0.0, max_value=1.0
                ),
                FieldSchema(
                    "price_target", FieldType.FLOAT, required=False, default=0.0
                ),
                FieldSchema("stop_loss", FieldType.FLOAT, required=False, default=0.0),
                FieldSchema("reasoning", FieldType.OPTIONAL_STRING, required=False),
                FieldSchema("tags", FieldType.LIST, required=False, default=[]),
            ],
        )
    )

    so.register(
        OutputSchema(
            name="cluster_health",
            description="Cluster health summary",
            fields=[
                FieldSchema(
                    "status",
                    FieldType.ENUM,
                    enum_values=["healthy", "degraded", "critical"],
                ),
                FieldSchema("nodes_up", FieldType.INTEGER, min_value=0),
                FieldSchema("nodes_total", FieldType.INTEGER, min_value=0),
                FieldSchema(
                    "gpu_temp_max", FieldType.FLOAT, required=False, default=0.0
                ),
                FieldSchema("alerts", FieldType.LIST, required=False, default=[]),
            ],
        )
    )

    return so


def main():
    import sys

    so = build_jarvis_structured_output()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        print("Structured output demo...\n")

        # Inference response — good
        r1 = so.validate(
            {
                "model": "qwen3.5",
                "response": "Hello!",
                "tokens_used": "42",
                "finish_reason": "stop",
            },
            "inference_response",
        )
        print(
            f"  inference OK={r1.valid} coerced={r1.coerced} defaults={r1.defaults_applied}"
        )

        # Inference — wrong finish_reason
        r2 = so.validate(
            {"model": "qwen3.5", "response": "Hi", "finish_reason": "invalid_reason"},
            "inference_response",
        )
        print(f"  inference bad enum OK={r2.valid} errors={r2.errors}")

        # Trading signal — from JSON string
        r3 = so.validate(
            '{"symbol": "BTC/USDT", "action": "BUY", "confidence": "0.85"}',
            "trading_signal",
        )
        print(
            f"  trading_signal OK={r3.valid} coerced={r3.coerced} data.action={r3.data.get('action')}"
        )

        # Cluster health — missing required
        r4 = so.validate(
            {"status": "healthy", "nodes_up": 3},
            "cluster_health",
        )
        print(f"  cluster_health OK={r4.valid} warnings={r4.warnings}")

        print(
            f"\nPrompt hint for inference_response:\n{so.prompt_hint('inference_response')[:300]}"
        )
        print(f"\nStats: {json.dumps(so.stats(), indent=2)}")

    elif cmd == "stats":
        print(json.dumps(so.stats(), indent=2))


if __name__ == "__main__":
    main()
