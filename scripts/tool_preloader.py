#!/usr/bin/env python3
"""
tool_preloader.py — Pré-charge tous les skills/agents/tools dans le contexte LLM local
Architecture: LLM local (M1/M2) porte le manifest complet → Claude dispatch sans charger

Usage:
  python3 tool_preloader.py --build    # Construit manifest JSON
  python3 tool_preloader.py --warm     # Envoie manifest à M1+M2 (pre-warm)
  python3 tool_preloader.py --dispatch "task description"  # Dispatch via LLM pré-chargé
"""

import json
import os
import subprocess
import sys
from pathlib import Path
import requests

JARVIS_ROOT = Path.home() / "IA/Core/jarvis"
SKILLS_DIR  = Path.home() / ".claude/skills"
MANIFEST_PATH = Path("/tmp/jarvis-tool-manifest.json")

M1 = "http://127.0.0.1:1234"
M2 = "http://192.168.1.26:1234"
MODEL_FAST = "qwen/qwen3.5-9b"
MODEL_BIG  = "qwen/qwen3.5-35b-a3b"

# ──────────────────────────────────────────────
# 1. BUILD MANIFEST
# ──────────────────────────────────────────────
def build_manifest() -> dict:
    manifest = {"skills": [], "agents": [], "commands": [], "chains": []}

    # Skills ~/.claude/skills/
    for skill_dir in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        name = skill_dir.parent.name
        if name.startswith("_"):
            continue
        content = skill_dir.read_text(errors="ignore")
        # Extract trigger + description (first 20 lines)
        summary = "\n".join(content.splitlines()[:20])
        manifest["skills"].append({"name": name, "summary": summary})

    # Skills dans jarvis repo
    for skill_file in (JARVIS_ROOT / "skills").rglob("SKILL.md"):
        name = skill_file.parent.name
        content = skill_file.read_text(errors="ignore")
        summary = "\n".join(content.splitlines()[:20])
        manifest["skills"].append({"name": f"jarvis:{name}", "summary": summary})

    # Agents (agent definitions)
    for agent_file in (JARVIS_ROOT / "agents").glob("*.md") if (JARVIS_ROOT / "agents").exists() else []:
        manifest["agents"].append({
            "name": agent_file.stem,
            "summary": agent_file.read_text(errors="ignore")[:300]
        })

    # LLM backends disponibles
    manifest["backends"] = _probe_backends()

    print(f"Manifest: {len(manifest['skills'])} skills, {len(manifest['agents'])} agents, "
          f"{len(manifest['backends'])} backends")
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest

def _probe_backends() -> list:
    backends = []
    for node, url in [("M1", M1), ("M2", M2)]:
        try:
            r = requests.get(f"{url}/v1/models", timeout=2)
            if r.ok:
                models = [m["id"] for m in r.json().get("data", [])]
                backends.append({"node": node, "url": url, "models": models[:5], "status": "up"})
        except Exception:
            backends.append({"node": node, "url": url, "models": [], "status": "down"})
    return backends

# ──────────────────────────────────────────────
# 2. PRE-WARM LLM avec le manifest
# ──────────────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """Tu es JARVIS-AGENT, un agent d'orchestration pré-chargé.

Tu connais TOUS ces outils disponibles dans le système JARVIS:

{manifest_summary}

Règles:
- Quand on te donne une tâche, identifie le skill/tool le plus adapté et exécute-le
- Tu peux appeler bash, lire des fichiers, faire des requêtes HTTP
- Réponds UNIQUEMENT avec le résultat de la tâche, pas d'explication
- Si la tâche nécessite un skill spécifique, utilise sa procédure exacte
"""

def warm_llm(manifest: dict):
    skills_summary = "\n".join(
        f"- SKILL:{s['name']}: {s['summary'][:100].replace(chr(10),' ')}"
        for s in manifest["skills"]
    )
    system = SYSTEM_PROMPT_TEMPLATE.format(manifest_summary=skills_summary)

    # Envoie une requête de "warm-up" à M1 et M2
    for node, url in [("M1", M1), ("M2", M2)]:
        try:
            payload = {
                "model": MODEL_FAST,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "ACK: tu es prêt? réponds juste 'READY'"}
                ],
                "max_tokens": 20,
                "temperature": 0
            }
            r = requests.post(f"{url}/v1/chat/completions", json=payload, timeout=30)
            if r.ok:
                resp = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                print(f"[{node}] Pre-warm OK: {resp.strip()[:50]}")
            else:
                print(f"[{node}] Pre-warm failed: {r.status_code}")
        except Exception as e:
            print(f"[{node}] Pre-warm error: {e}")

# ──────────────────────────────────────────────
# 3. DISPATCH via LLM pré-chargé
# ──────────────────────────────────────────────
def dispatch(task: str, model: str = MODEL_FAST, node: str = "M1"):
    if not MANIFEST_PATH.exists():
        build_manifest()

    manifest = json.loads(MANIFEST_PATH.read_text())
    skills_summary = "\n".join(
        f"- {s['name']}: {s['summary'][:80].replace(chr(10),' ')}"
        for s in manifest["skills"]
    )
    system = SYSTEM_PROMPT_TEMPLATE.format(manifest_summary=skills_summary)
    url = M1 if node == "M1" else M2

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": task}
        ],
        "max_tokens": 2000,
        "temperature": 0.2
    }
    try:
        r = requests.post(f"{url}/v1/chat/completions", json=payload, timeout=120)
        if r.ok:
            return r.json()["choices"][0]["message"]["content"]
        return f"Error {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"Dispatch error: {e}"

# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--build"

    if cmd == "--build":
        m = build_manifest()
        print(f"Manifest saved: {MANIFEST_PATH}")
        print(json.dumps(m, indent=2, ensure_ascii=False)[:500])

    elif cmd == "--warm":
        if not MANIFEST_PATH.exists():
            build_manifest()
        m = json.loads(MANIFEST_PATH.read_text())
        warm_llm(m)

    elif cmd == "--dispatch":
        task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "status"
        result = dispatch(task)
        print(result)

    elif cmd == "--all":
        m = build_manifest()
        warm_llm(m)
        print("Pre-warm complete. Use --dispatch 'task' to route tasks.")

# ──────────────────────────────────────────────
# PATCH: backends étendus OL1 + Ollama Cloud
# ──────────────────────────────────────────────
OL1 = "http://127.0.0.1:11434"
OLLAMA_CLOUD = "https://ollama.com/v1"
OLLAMA_CLOUD_KEY = os.environ.get("OLLAMA_API_KEY", "5ed25086692848798d4322d4c9861d57.-KFfHj-wHbNma_PyCrWdr9hQ")

# Routing table: quel modèle pour quel type de tâche
ROUTING_TABLE = {
    "fast":     {"node": "M1",    "url": M1,    "model": "qwen/qwen3.5-9b"},
    "big":      {"node": "M1",    "url": M1,    "model": "qwen/qwen3.5-35b-a3b"},
    "reason":   {"node": "M1",    "url": M1,    "model": "deepseek/deepseek-r1-0528-qwen3-8b"},
    "code":     {"node": "M1",    "url": M1,    "model": "qwen/qwen2.5-coder-14b"},
    "distilled":{"node": "M1",    "url": M1,    "model": "qwen/qwen3.5-9b"},  # + distillés quand chargés
    "light":    {"node": "OL1",   "url": OL1,   "model": "gemma3:4b"},
    "kimi":     {"node": "OL1",   "url": OL1,   "model": "kimi-k2.5:cloud"},  # cloud via Ollama
    "gpt":      {"node": "OL1",   "url": OL1,   "model": "kimi-k2.5:cloud"},  # fallback
}

def dispatch_smart(task: str, mode: str = "fast") -> str:
    """Dispatch intelligent selon le type de tâche."""
    route = ROUTING_TABLE.get(mode, ROUTING_TABLE["fast"])
    
    if not MANIFEST_PATH.exists():
        build_manifest()
    manifest = json.loads(MANIFEST_PATH.read_text())
    skills_summary = "\n".join(
        f"- {s['name']}: {s['summary'][:80].replace(chr(10),' ')}"
        for s in manifest["skills"]
    )
    system = SYSTEM_PROMPT_TEMPLATE.format(manifest_summary=skills_summary)

    # Appel Ollama vs LMStudio
    if route["node"] == "OL1":
        payload = {
            "model": route["model"],
            "prompt": f"{system}\n\nTâche: {task}",
            "stream": False
        }
        try:
            r = requests.post(f"{OL1}/api/generate", json=payload, timeout=60)
            return r.json().get("response", "") if r.ok else f"OL1 error: {r.status_code}"
        except Exception as e:
            return f"OL1 error: {e}"
    else:
        payload = {
            "model": route["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task}
            ],
            "max_tokens": 2000,
            "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False}
        }
        try:
            r = requests.post(f"{route['url']}/v1/chat/completions", json=payload, timeout=120)
            return r.json()["choices"][0]["message"]["content"] if r.ok else f"Error: {r.status_code}"
        except Exception as e:
            return f"Error: {e}"
