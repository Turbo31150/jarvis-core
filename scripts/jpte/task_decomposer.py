#!/usr/bin/env python3
"""
JPTE — Task Decomposer
Découpe une demande utilisateur en N tâches parallèles (max 10).
Utilise lm-ask.sh (0 token Claude) pour la décomposition.
Usage: python3 task_decomposer.py "ma demande" [--session <sid>]
"""

import json
import subprocess
import sys
import re
from todolist_engine import create_session, add_task

AGENT_MAP = {
    "audit": "lm-ask.sh --reason",
    "analyse_code": "feature-dev:code-explorer",
    "implementation": "omega-dev-agent",
    "documentation": "omega-docs-agent",
    "securite": "omega-security-agent",
    "infra": "omega-system-agent",
    "recherche": "omega-analysis-agent",
    "git": "commit-commands:commit",
    "trading": "omega-trading-agent",
    "correction": "lm-ask.sh --reason",
    "meta": "lm-ask.sh",
}

DECOMPOSE_PROMPT = """Tu es un orchestrateur de tâches IA. Décompose cette demande en 2 à 10 tâches parallèles.

DEMANDE: {request}

Réponds UNIQUEMENT en JSON valide, format exact:
{{
  "taches": [
    {{
      "id": 1,
      "titre": "titre court",
      "description": "description détaillée de ce que fait cette tâche",
      "type": "audit|action|correction|meta",
      "sous_type": "analyse_code|implementation|documentation|securite|infra|recherche|git|trading",
      "priorite": 1,
      "complexite": 3,
      "depends_on": []
    }}
  ]
}}

Types:
- audit: analyser l'existant avant d'agir
- action: exécuter une tâche concrète
- correction: valider et corriger le résultat d'une action
- meta: tâches transversales (commit, update mémoire)

Priorité: 1=critique, 2=normal, 3=faible
Complexité: 1-5
depends_on: liste des IDs de tâches dont celle-ci dépend (IDs numériques de cette liste)
"""


def decompose_with_llm(request: str) -> list[dict]:
    prompt = DECOMPOSE_PROMPT.format(request=request)
    try:
        result = subprocess.run(
            ["bash", "/home/turbo/jarvis/scripts/lm-ask.sh", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = result.stdout.strip()
        # Extract JSON
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("taches", [])
    except Exception:
        pass
    return _fallback_decompose(request)


def _fallback_decompose(request: str) -> list[dict]:
    """Fallback si LLM indisponible — décomposition générique."""
    return [
        {
            "id": 1,
            "titre": "Audit initial",
            "description": f"Analyser l'existant pour: {request[:50]}",
            "type": "audit",
            "sous_type": "audit",
            "priorite": 1,
            "complexite": 2,
            "depends_on": [],
        },
        {
            "id": 2,
            "titre": "Exécution principale",
            "description": f"Réaliser: {request[:50]}",
            "type": "action",
            "sous_type": "implementation",
            "priorite": 2,
            "complexite": 3,
            "depends_on": [1],
        },
        {
            "id": 3,
            "titre": "Validation et correction",
            "description": "Valider le résultat et corriger si nécessaire",
            "type": "correction",
            "sous_type": "correction",
            "priorite": 2,
            "complexite": 2,
            "depends_on": [2],
        },
        {
            "id": 4,
            "titre": "Finalisation",
            "description": "Commit, update mémoire, documentation",
            "type": "meta",
            "sous_type": "git",
            "priorite": 3,
            "complexite": 1,
            "depends_on": [3],
        },
    ]


def decompose(request: str, session_id: str | None = None) -> tuple[str, list[str]]:
    if not session_id:
        session_id = create_session(request)

    taches = decompose_with_llm(request)

    # Map local IDs → real task IDs
    id_map: dict[int, str] = {}
    task_ids = []

    for t in taches:
        local_id = t["id"]
        depends_on_real = [id_map[d] for d in t.get("depends_on", []) if d in id_map]
        sous_type = t.get("sous_type", t.get("type", "action"))
        agent = AGENT_MAP.get(sous_type, "omega-dev-agent")

        tid = add_task(
            session_id=session_id,
            title=t["titre"],
            description=t.get("description", ""),
            task_type=t.get("type", "action"),
            priority=t.get("priorite", 2),
            complexity=t.get("complexite", 3),
            agent=agent,
            depends_on=depends_on_real,
        )
        id_map[local_id] = tid
        task_ids.append(tid)

    return session_id, task_ids


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: task_decomposer.py <demande> [--session <sid>]")
        sys.exit(1)

    request = sys.argv[1]
    sid = None
    if "--session" in sys.argv:
        idx = sys.argv.index("--session")
        sid = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    session_id, task_ids = decompose(request, sid)
    print(json.dumps({"session_id": session_id, "task_ids": task_ids}, indent=2))
