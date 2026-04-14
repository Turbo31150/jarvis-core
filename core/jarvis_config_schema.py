#!/usr/bin/env python3
"""
jarvis_config_schema — JSON Schema validation and typed config loading for JARVIS
Validates cluster configs, provides typed access, and detects drift from schema
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

log = logging.getLogger("jarvis.config_schema")

REDIS_PREFIX = "jarvis:cfgschema:"
CONFIG_FILE = Path("/home/turbo/IA/Core/jarvis/data/jarvis_config.json")


class SchemaType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    ANY = "any"


@dataclass
class SchemaField:
    name: str
    type: SchemaType
    required: bool = False
    default: Any = None
    description: str = ""
    min_val: float | None = None
    max_val: float | None = None
    min_len: int | None = None
    max_len: int | None = None
    pattern: str | None = None
    allowed: list[Any] | None = None
    env_var: str | None = None  # load from environment variable if set
    nested: list["SchemaField"] | None = None  # for OBJECT type


@dataclass
class Schema:
    name: str
    version: str = "1.0"
    fields: list[SchemaField] = field(default_factory=list)
    allow_extra: bool = True

    def field_map(self) -> dict[str, SchemaField]:
        return {f.name: f for f in self.fields}


@dataclass
class ValidationIssue:
    path: str
    severity: str  # "error" | "warning"
    message: str

    def to_dict(self) -> dict:
        return {"path": self.path, "severity": self.severity, "message": self.message}


@dataclass
class ValidationReport:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)
    coerced: dict[str, Any] = field(default_factory=dict)
    missing_with_defaults: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [i.to_dict() for i in self.issues if i.severity == "error"],
            "warnings": [i.to_dict() for i in self.issues if i.severity == "warning"],
            "coerced": list(self.coerced.keys()),
            "defaults_applied": self.missing_with_defaults,
        }


def _coerce_value(value: Any, target_type: SchemaType) -> tuple[Any, bool]:
    try:
        if target_type == SchemaType.STRING:
            return str(value), True
        elif target_type == SchemaType.INTEGER:
            return int(value), True
        elif target_type == SchemaType.FLOAT:
            return float(value), True
        elif target_type == SchemaType.BOOLEAN:
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes", "on"), True
            return bool(value), True
    except (ValueError, TypeError):
        pass
    return value, False


_TYPE_MAP = {
    SchemaType.STRING: str,
    SchemaType.INTEGER: int,
    SchemaType.FLOAT: float,
    SchemaType.BOOLEAN: bool,
    SchemaType.ARRAY: list,
    SchemaType.OBJECT: dict,
}


def _validate_field(
    path: str,
    value: Any,
    sf: SchemaField,
    issues: list[ValidationIssue],
    coerced: dict[str, Any],
    auto_coerce: bool = True,
) -> Any:
    if value is None:
        return value

    # Type check
    expected = _TYPE_MAP.get(sf.type)
    if expected and sf.type != SchemaType.ANY and not isinstance(value, expected):
        if auto_coerce:
            new_val, ok = _coerce_value(value, sf.type)
            if ok:
                coerced[path] = new_val
                value = new_val
            else:
                issues.append(
                    ValidationIssue(
                        path,
                        "error",
                        f"Expected {sf.type.value}, got {type(value).__name__}",
                    )
                )
                return value
        else:
            issues.append(
                ValidationIssue(
                    path,
                    "error",
                    f"Expected {sf.type.value}, got {type(value).__name__}",
                )
            )

    # Range checks
    if sf.min_val is not None and isinstance(value, (int, float)):
        if value < sf.min_val:
            issues.append(
                ValidationIssue(path, "error", f"Value {value} < min {sf.min_val}")
            )
    if sf.max_val is not None and isinstance(value, (int, float)):
        if value > sf.max_val:
            issues.append(
                ValidationIssue(path, "error", f"Value {value} > max {sf.max_val}")
            )

    # Length checks
    if hasattr(value, "__len__"):
        if sf.min_len is not None and len(value) < sf.min_len:
            issues.append(
                ValidationIssue(
                    path, "error", f"Length {len(value)} < min {sf.min_len}"
                )
            )
        if sf.max_len is not None and len(value) > sf.max_len:
            issues.append(
                ValidationIssue(
                    path, "error", f"Length {len(value)} > max {sf.max_len}"
                )
            )

    # Pattern
    if sf.pattern and isinstance(value, str):
        if not re.fullmatch(sf.pattern, value):
            issues.append(
                ValidationIssue(path, "error", f"Doesn't match pattern '{sf.pattern}'")
            )

    # Enum
    if sf.allowed and value not in sf.allowed:
        issues.append(
            ValidationIssue(path, "error", f"Value {value!r} not in {sf.allowed}")
        )

    # Nested object
    if sf.nested and isinstance(value, dict):
        for nested_sf in sf.nested:
            npath = f"{path}.{nested_sf.name}"
            nval = value.get(nested_sf.name)
            if nval is None:
                if nested_sf.required:
                    if nested_sf.default is not None:
                        value[nested_sf.name] = nested_sf.default
                    else:
                        issues.append(
                            ValidationIssue(npath, "error", "Required field missing")
                        )
            else:
                value[nested_sf.name] = _validate_field(
                    npath, nval, nested_sf, issues, coerced, auto_coerce
                )

    return value


class ConfigSchema:
    def __init__(self):
        self.redis: aioredis.Redis | None = None
        self._schemas: dict[str, Schema] = {}
        self._loaded_configs: dict[str, dict] = {}
        self._stats: dict[str, int] = {"validations": 0, "errors": 0, "loads": 0}
        self._register_jarvis_schema()

    async def connect_redis(self):
        try:
            self.redis = aioredis.Redis(decode_responses=True)
            await self.redis.ping()
        except Exception:
            self.redis = None

    def _register_jarvis_schema(self):
        node_schema = SchemaField(
            "node",
            SchemaType.OBJECT,
            nested=[
                SchemaField(
                    "url", SchemaType.STRING, required=True, pattern=r"https?://.+"
                ),
                SchemaField(
                    "weight", SchemaType.FLOAT, default=1.0, min_val=0.0, max_val=10.0
                ),
                SchemaField("enabled", SchemaType.BOOLEAN, default=True),
                SchemaField(
                    "timeout_s",
                    SchemaType.FLOAT,
                    default=30.0,
                    min_val=1.0,
                    max_val=300.0,
                ),
            ],
        )

        schema = Schema(
            "jarvis_cluster",
            version="2.0",
            fields=[
                SchemaField("version", SchemaType.STRING, required=True),
                SchemaField(
                    "log_level",
                    SchemaType.STRING,
                    default="INFO",
                    allowed=["DEBUG", "INFO", "WARNING", "ERROR"],
                ),
                SchemaField("cluster", SchemaType.OBJECT, required=True),
                SchemaField(
                    "redis_url",
                    SchemaType.STRING,
                    default="redis://localhost:6379",
                    env_var="REDIS_URL",
                ),
                SchemaField(
                    "max_concurrency",
                    SchemaType.INTEGER,
                    default=8,
                    min_val=1,
                    max_val=128,
                ),
                SchemaField(
                    "default_timeout_s", SchemaType.FLOAT, default=30.0, min_val=1.0
                ),
                SchemaField("features", SchemaType.OBJECT, default={}),
            ],
        )
        self.register(schema)

    def register(self, schema: Schema):
        self._schemas[schema.name] = schema

    def validate(
        self, config: dict, schema_name: str, auto_coerce: bool = True
    ) -> ValidationReport:
        self._stats["validations"] += 1
        schema = self._schemas.get(schema_name)
        if not schema:
            return ValidationReport(
                False,
                [ValidationIssue("_schema", "error", f"Unknown schema: {schema_name}")],
            )

        issues: list[ValidationIssue] = []
        coerced: dict[str, Any] = {}
        defaults_applied: list[str] = []
        data = dict(config)

        field_map = schema.field_map()

        # Check unknown fields
        if not schema.allow_extra:
            for key in data:
                if key not in field_map:
                    issues.append(
                        ValidationIssue(key, "warning", f"Unknown field: {key}")
                    )

        for sf in schema.fields:
            # Check env var override
            if sf.env_var:
                env_val = os.environ.get(sf.env_var)
                if env_val:
                    data[sf.name] = env_val

            val = data.get(sf.name)
            if val is None:
                if sf.required:
                    if sf.default is not None:
                        data[sf.name] = sf.default
                        defaults_applied.append(sf.name)
                    else:
                        issues.append(
                            ValidationIssue(sf.name, "error", "Required field missing")
                        )
                elif sf.default is not None:
                    data[sf.name] = sf.default
                    defaults_applied.append(sf.name)
                continue

            data[sf.name] = _validate_field(
                sf.name, val, sf, issues, coerced, auto_coerce
            )

        errors = [i for i in issues if i.severity == "error"]
        if errors:
            self._stats["errors"] += 1

        return ValidationReport(
            valid=len(errors) == 0,
            issues=issues,
            coerced=coerced,
            missing_with_defaults=defaults_applied,
        )

    def load_file(
        self, path: Path | None = None, schema_name: str = "jarvis_cluster"
    ) -> tuple[dict, ValidationReport]:
        p = path or CONFIG_FILE
        self._stats["loads"] += 1
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError as e:
            return {}, ValidationReport(
                False, [ValidationIssue("_file", "error", f"JSON parse error: {e}")]
            )

        report = self.validate(data, schema_name)
        if report.valid:
            self._loaded_configs[schema_name] = data
        return data, report

    def get(
        self, key: str, schema_name: str = "jarvis_cluster", default: Any = None
    ) -> Any:
        config = self._loaded_configs.get(schema_name, {})
        return config.get(key, default)

    def stats(self) -> dict:
        return {**self._stats, "schemas": len(self._schemas)}


def build_jarvis_config_schema() -> ConfigSchema:
    return ConfigSchema()


async def main():
    import sys

    cs = build_jarvis_config_schema()
    await cs.connect_redis()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"

    if cmd == "demo":
        valid_config = {
            "version": "2.0",
            "log_level": "INFO",
            "cluster": {
                "m1": {"url": "http://192.168.1.85:1234", "weight": 1.2},
                "m2": {"url": "http://192.168.1.26:1234", "weight": 1.0},
            },
            "max_concurrency": "8",  # string — should be coerced
        }
        report = cs.validate(valid_config, "jarvis_cluster")
        print(f"Valid config: {report.valid}")
        print(f"  Coerced: {list(report.coerced.keys())}")
        print(f"  Defaults applied: {report.missing_with_defaults}")

        invalid_config = {
            "log_level": "VERBOSE",  # not in allowed list
            "max_concurrency": 200,  # too high
            # missing required "cluster"
        }
        report2 = cs.validate(invalid_config, "jarvis_cluster")
        print(f"\nInvalid config: {report2.valid}")
        for err in [i for i in report2.issues if i.severity == "error"]:
            print(f"  ❌ {err.path}: {err.message}")
        for warn in [i for i in report2.issues if i.severity == "warning"]:
            print(f"  ⚠️ {warn.path}: {warn.message}")

        print(f"\nStats: {json.dumps(cs.stats(), indent=2)}")

    elif cmd == "validate":
        path = Path(sys.argv[2]) if len(sys.argv) > 2 else CONFIG_FILE
        data, report = cs.load_file(path)
        print(json.dumps(report.to_dict(), indent=2))

    elif cmd == "stats":
        print(json.dumps(cs.stats(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
