#!/usr/bin/env python3
"""
lm_guard.py — Enforcement runtime des règles LM Studio / cluster
Vérifie VRAM, modèles chargés, cascade, avant tout appel LLM.
Exécuté par lm-ask.sh et les services JARVIS avant inference.

Usage:
  python3 lm_guard.py check          → audit rapide, exit 0=ok 1=warning 2=critical
  python3 lm_guard.py enforce        → applique les règles (unload, TTL, cascade)
  python3 lm_guard.py status         → rapport complet JSON
"""

import subprocess
import json
import sys
import time
import urllib.request
import urllib.error

# ── Règles configurables ────────────────────────────────────────────────
VRAM_WARN_PCT = 80  # % VRAM → warning
VRAM_CRIT_PCT = 85  # % VRAM → block chargement nouveau modèle
TEMP_CRIT_C = 85  # °C → block GPU ops
MAX_IDLE_MODELS = 2  # max modèles IDLE simultanés (hors actifs)
TTL_IDLE_MIN = 30  # TTL à appliquer sur modèles idle sans TTL (minutes)

# Modèles autorisés par rôle (règle cascade CLAUDE.md)
ROLE_MODELS = {
    "fast": ["qwen/qwen3.5-9b", "qwen/qwen3-8b", "gemma3:4b", "llama3.2"],
    "reason": ["deepseek/deepseek-r1-0528-qwen3-8b", "deepseek-r1:7b"],
    "big": ["qwen/qwen3.5-35b-a3b"],  # seulement sur demande explicite
    "code": ["qwen/qwen2.5-coder-14b"],
    "embed": ["text-embedding-nomic-embed-text-v1.5", "nomic-embed-text:latest"],
}

# Modèles qui ne doivent PAS rester IDLE > 10min (trop lourds)
HEAVY_MODELS = ["qwen/qwen3.5-35b-a3b"]

LMS = "/home/turbo/.lmstudio/bin/lms"

# ── Helpers ─────────────────────────────────────────────────────────────


def gpu_stats():
    """Retourne liste de dicts par GPU."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,memory.total,temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
        gpus = []
        for line in out.strip().split("\n"):
            idx, used, total, temp, util = [x.strip() for x in line.split(",")]
            gpus.append(
                {
                    "index": int(idx),
                    "used_mb": int(used),
                    "total_mb": int(total),
                    "pct": round(int(used) / int(total) * 100, 1),
                    "temp_c": int(temp),
                    "util_pct": int(util),
                }
            )
        return gpus
    except Exception:
        return []


def lms_ps():
    """Retourne les modèles chargés via lms ps."""
    try:
        out = subprocess.check_output([LMS, "ps"], text=True, timeout=5)
        models = []
        for line in out.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 3:
                models.append(
                    {
                        "identifier": parts[0],
                        "status": parts[2],
                        "has_ttl": "/" in line and "m /" in line,
                        "line": line,
                    }
                )
        return models
    except Exception:
        return []


def lms_unload(identifier):
    try:
        out = subprocess.check_output(
            [LMS, "unload", identifier], text=True, timeout=10, stderr=subprocess.STDOUT
        )
        return True, out.strip()
    except subprocess.CalledProcessError as e:
        return False, e.output


def node_up(url):
    try:
        urllib.request.urlopen(f"{url}/v1/models", timeout=3)
        return True
    except Exception:
        return False


# ── Checks ──────────────────────────────────────────────────────────────


def check_vram(gpus):
    issues = []
    for g in gpus:
        if g["pct"] >= VRAM_CRIT_PCT:
            issues.append(
                {
                    "level": "CRITICAL",
                    "gpu": g["index"],
                    "msg": f"GPU{g['index']} VRAM {g['pct']}% >= {VRAM_CRIT_PCT}%",
                }
            )
        elif g["pct"] >= VRAM_WARN_PCT:
            issues.append(
                {
                    "level": "WARN",
                    "gpu": g["index"],
                    "msg": f"GPU{g['index']} VRAM {g['pct']}% >= {VRAM_WARN_PCT}%",
                }
            )
        if g["temp_c"] >= TEMP_CRIT_C:
            issues.append(
                {
                    "level": "CRITICAL",
                    "gpu": g["index"],
                    "msg": f"GPU{g['index']} temp {g['temp_c']}°C >= {TEMP_CRIT_C}°C",
                }
            )
    return issues


def check_loaded_models(models):
    issues = []
    idle = [m for m in models if m["status"] == "IDLE"]
    active = [m for m in models if m["status"] in ("GENERATING", "PROCESSINGPROMPT")]

    if len(idle) > MAX_IDLE_MODELS:
        issues.append(
            {
                "level": "WARN",
                "msg": f"{len(idle)} modèles IDLE (max={MAX_IDLE_MODELS}): {[m['identifier'] for m in idle]}",
            }
        )

    for m in models:
        name = m["identifier"].split(":")[0]
        if name in HEAVY_MODELS and m["status"] == "IDLE":
            issues.append(
                {
                    "level": "WARN",
                    "msg": f"Modèle lourd IDLE sans usage: {m['identifier']} (devrait être déchargé)",
                }
            )

        if m["status"] == "IDLE" and not m["has_ttl"]:
            issues.append(
                {
                    "level": "WARN",
                    "msg": f"Modèle IDLE sans TTL: {m['identifier']} (risque de rester chargé)",
                }
            )

    # Doublons IDLE uniquement (multi-device GENERATING = légitime)
    idle_by_base = {}
    for m in models:
        if m["status"] != "IDLE":
            continue
        base = m["identifier"].split(":")[0]
        idle_by_base[base] = idle_by_base.get(base, 0) + 1
    for base, count in idle_by_base.items():
        if count > 1:
            issues.append(
                {
                    "level": "WARN",
                    "msg": f"Modèle IDLE en {count} copies: {base} (doublons inutiles)",
                }
            )
    return issues


def check_cascade():
    """Vérifie disponibilité des nœuds dans l'ordre de la cascade."""
    nodes = [
        ("M1", "http://192.168.1.85:1234"),
        ("M2", "http://192.168.1.26:1234"),
        ("M32", "http://192.168.1.113:1234"),
        ("OL1", "http://127.0.0.1:11434"),
    ]
    status = {}
    for name, url in nodes:
        status[name] = node_up(url)
    return status


# ── Enforce ─────────────────────────────────────────────────────────────


def enforce(models, gpus):
    actions = []
    ACTIVE = ("GENERATING", "PROCESSINGPROMPT")
    idle = [m for m in models if m["status"] == "IDLE"]

    # Unload modèles lourds IDLE uniquement (jamais toucher GENERATING)
    for m in idle:
        base = m["identifier"].split(":")[0]
        if base in HEAVY_MODELS:
            ok, msg = lms_unload(m["identifier"])
            actions.append(
                {
                    "action": "unload_heavy",
                    "model": m["identifier"],
                    "reason": "heavy IDLE",
                    "ok": ok,
                    "msg": msg,
                }
            )

    # Unload doublons IDLE
    seen = {}
    for m in models:
        if m["status"] in ACTIVE:
            continue  # jamais toucher un modèle actif
        base = m["identifier"].split(":")[0]
        if base not in seen:
            seen[base] = m
        else:
            ok, msg = lms_unload(m["identifier"])
            actions.append(
                {
                    "action": "unload_duplicate",
                    "model": m["identifier"],
                    "ok": ok,
                    "msg": msg,
                }
            )

    # Excédent IDLE après dédoublonnage
    idle2 = [m for m in lms_ps() if m["status"] == "IDLE"]
    if len(idle2) > MAX_IDLE_MODELS:
        for m in idle2[MAX_IDLE_MODELS:]:
            ok, msg = lms_unload(m["identifier"])
            actions.append(
                {
                    "action": "unload_excess",
                    "model": m["identifier"],
                    "ok": ok,
                    "msg": msg,
                }
            )

    return actions


# ── Main ────────────────────────────────────────────────────────────────


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"

    gpus = gpu_stats()
    models = lms_ps()
    vram_issues = check_vram(gpus)
    model_issues = check_loaded_models(models)
    cascade = check_cascade()

    all_issues = vram_issues + model_issues
    has_critical = any(i["level"] == "CRITICAL" for i in all_issues)
    has_warn = any(i["level"] == "WARN" for i in all_issues)

    if cmd == "check":
        if not all_issues:
            print("✅ GUARD OK — VRAM, modèles, cascade conformes")
            sys.exit(0)
        for i in all_issues:
            icon = "🔴" if i["level"] == "CRITICAL" else "⚠️"
            print(f"{icon} [{i['level']}] {i['msg']}")
        sys.exit(2 if has_critical else 1)

    elif cmd == "enforce":
        actions = enforce(models, gpus)
        if not actions:
            print("✅ Rien à corriger")
        for a in actions:
            icon = "✅" if a["ok"] else "❌"
            print(f"{icon} {a['action']}: {a['model']} — {a['msg']}")
        # Re-check après enforcement
        time.sleep(2)
        models2 = lms_ps()
        vram2 = check_vram(gpu_stats())
        model2 = check_loaded_models(models2)
        remaining = vram2 + model2
        if remaining:
            print(f"\n⚠️ {len(remaining)} issues restants après enforcement")
            sys.exit(1)
        else:
            print("\n✅ Toutes règles respectées après enforcement")
            sys.exit(0)

    elif cmd == "status":
        out = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "gpus": gpus,
            "loaded_models": [
                m["identifier"] + " [" + m["status"] + "]" for m in models
            ],
            "issues": all_issues,
            "cascade": cascade,
            "compliant": len(all_issues) == 0,
        }
        print(json.dumps(out, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
