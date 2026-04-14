#!/usr/bin/env python3
"""JARVIS Response Validator — Validate LLM responses against JSON schemas"""

import json
import re
import redis

r = redis.Redis(decode_responses=True)

SCHEMAS = {
    "trading_signal": {
        "required": ["action", "symbol", "confidence"],
        "types": {"action": str, "symbol": str, "confidence": (int, float)},
        "enums": {"action": ["BUY", "SELL", "HOLD"]},
        "ranges": {"confidence": (0.0, 1.0)},
    },
    "health_report": {
        "required": ["status", "score"],
        "types": {"status": str, "score": (int, float)},
        "enums": {"status": ["ok", "warn", "critical"]},
        "ranges": {"score": (0, 100)},
    },
    "task_plan": {
        "required": ["steps"],
        "types": {"steps": list},
    },
    "classification": {
        "required": ["label", "confidence"],
        "types": {"label": str, "confidence": (int, float)},
        "ranges": {"confidence": (0.0, 1.0)},
    },
}


def extract_json(text: str) -> dict | None:
    """Try to extract JSON from LLM response text."""
    # Direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    # JSON block extraction
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Inline JSON
    m = re.search(r"\{[^{}]+\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


def validate(data: dict, schema_name: str) -> dict:
    """Validate data against a named schema. Returns {valid, errors}."""
    schema = SCHEMAS.get(schema_name)
    if not schema:
        return {"valid": False, "errors": [f"unknown schema: {schema_name}"]}

    errors = []
    for field in schema.get("required", []):
        if field not in data:
            errors.append(f"missing field: {field}")

    for field, expected_type in schema.get("types", {}).items():
        if field in data and not isinstance(data[field], expected_type):
            errors.append(f"{field}: expected {expected_type}, got {type(data[field])}")

    for field, allowed in schema.get("enums", {}).items():
        if field in data and data[field] not in allowed:
            errors.append(f"{field}: must be one of {allowed}, got {data[field]!r}")

    for field, (lo, hi) in schema.get("ranges", {}).items():
        if field in data:
            v = data[field]
            if not (lo <= v <= hi):
                errors.append(f"{field}: {v} out of range [{lo}, {hi}]")

    result = {"valid": len(errors) == 0, "errors": errors}
    r.incr(f"jarvis:validator:{schema_name}:{'ok' if result['valid'] else 'fail'}")
    return result


def validate_llm_response(text: str, schema_name: str) -> dict:
    """Extract JSON from LLM text and validate it."""
    data = extract_json(text)
    if data is None:
        r.incr(f"jarvis:validator:{schema_name}:parse_fail")
        return {
            "valid": False,
            "errors": ["no JSON found in response"],
            "raw": text[:200],
        }
    result = validate(data, schema_name)
    result["data"] = data
    return result


def stats() -> dict:
    res = {}
    for schema in SCHEMAS:
        ok = int(r.get(f"jarvis:validator:{schema}:ok") or 0)
        fail = int(r.get(f"jarvis:validator:{schema}:fail") or 0)
        pf = int(r.get(f"jarvis:validator:{schema}:parse_fail") or 0)
        if ok + fail + pf > 0:
            res[schema] = {"ok": ok, "fail": fail, "parse_fail": pf}
    return res


if __name__ == "__main__":
    tests = [
        ('{"action": "BUY", "symbol": "BTC", "confidence": 0.85}', "trading_signal"),
        ('{"action": "MAYBE", "symbol": "ETH", "confidence": 1.5}', "trading_signal"),
        (
            'Here is the result: {"label": "positive", "confidence": 0.9}',
            "classification",
        ),
        ("No JSON here at all", "health_report"),
    ]
    for text, schema in tests:
        r_ = validate_llm_response(text, schema)
        icon = "✅" if r_["valid"] else "❌"
        print(f"{icon} [{schema}] {r_['errors'] or 'ok'}")
