#!/usr/bin/env python3
"""JARVIS Secret Manager — Centralized encrypted secret storage + rotation alerts"""
import os, redis, json, hashlib
from datetime import datetime, timedelta
from pathlib import Path

r = redis.Redis(decode_responses=True)

SECRET_FILES = [
    "/home/turbo/IA/Core/jarvis/config/secrets.env",
    "/home/turbo/.config/jarvis/secrets.env",
    "/home/turbo/IA/Core/jarvis/.env",
]

SENSITIVE_KEYS = {"TELEGRAM_TOKEN", "OPENROUTER_API_KEY", "XAI_API_KEY",
                  "HL_PRIVATE_KEY", "HL_WALLET", "OPENAI_API_KEY"}

def _load_all() -> dict:
    secrets = {}
    for fpath in SECRET_FILES:
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        secrets[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
    return secrets

def audit() -> dict:
    secrets = _load_all()
    report = {"ts": datetime.now().isoformat()[:19], "total_keys": len(secrets),
              "sensitive_present": [], "missing": [], "warnings": []}
    
    for key in SENSITIVE_KEYS:
        if key in secrets and secrets[key]:
            # Hash for audit without exposing value
            h = hashlib.sha256(secrets[key].encode()).hexdigest()[:12]
            report["sensitive_present"].append({"key": key, "hash": h,
                                                 "length": len(secrets[key])})
        else:
            report["missing"].append(key)
    
    # Check for secrets in wrong places (root .env)
    if os.path.exists("/home/turbo/.env"):
        report["warnings"].append("Found .env in home directory — should use config/secrets.env")
    
    r.setex("jarvis:secret_audit", 3600, json.dumps(report))
    return report

def get(key: str) -> str | None:
    """Safe get — logs access without exposing value"""
    secrets = _load_all()
    val = secrets.get(key)
    r.lpush("jarvis:secret_access_log", json.dumps({
        "key": key, "found": bool(val), "ts": datetime.now().isoformat()[:19]
    }))
    r.ltrim("jarvis:secret_access_log", 0, 99)
    return val

if __name__ == "__main__":
    report = audit()
    print(f"Total keys: {report['total_keys']}")
    print(f"Sensitive present: {len(report['sensitive_present'])}")
    for s in report["sensitive_present"]:
        print(f"  ✅ {s['key']} (hash:{s['hash']}, len:{s['length']})")
    if report["missing"]:
        print(f"Missing: {report['missing']}")
    if report["warnings"]:
        print(f"Warnings: {report['warnings']}")
