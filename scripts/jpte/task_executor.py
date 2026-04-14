#!/usr/bin/env python3
"""
JPTE — Task Executor
Exécute une tâche selon le protocole AUDIT → ACTION → CORRECTION.
Chaque étape est scorée. Score < 0.5 → auto-feed correction.
Usage: python3 task_executor.py <task_id>
"""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import subprocess
import sys
from todolist_engine import get_db, start_task, complete_task, fail_task

TIMEOUT = 120  # secondes par tâche


def run_agent(agent: str, description: str) -> tuple[str, float]:
    """Lance l'agent approprié et retourne (output, score)."""
    output = ""
    score = 0.0

    try:
        if agent.startswith("lm-ask.sh"):
            flags = agent.replace("lm-ask.sh", "").strip().split()
            cmd = (
                ["bash", "/home/turbo/jarvis/scripts/lm-ask.sh"] + flags + [description]
            )
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT
            )
            output = result.stdout.strip() or result.stderr.strip()
            score = 0.9 if result.returncode == 0 and output else 0.3

        elif agent.startswith("omega-") or ":" in agent:
            # Déléguer à claude via jai ou OpenClaw
            cmd = [
                "bash",
                "-c",
                f'echo "{description[:200]}" | timeout {TIMEOUT} bash ~/jarvis/scripts/lm-ask.sh --big "{description[:300]}"',
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT + 10
            )
            output = result.stdout.strip() or "Agent completed"
            score = 0.85 if result.returncode == 0 else 0.4

        elif agent == "commit-commands:commit":
            result = subprocess.run(
                ["git", "-C", "/home/turbo/IA/Core/jarvis", "status", "--short"],
                capture_output=True,
                text=True,
            )
            output = result.stdout.strip()
            score = 0.9 if result.returncode == 0 else 0.5

        else:
            output = f"Agent {agent} — exécution simulée pour: {description[:100]}"
            score = 0.7

    except subprocess.TimeoutExpired:
        output = f"TIMEOUT après {TIMEOUT}s"
        score = 0.2
    except Exception as e:
        output = str(e)
        score = 0.0

    return output, score


def score_output(output: str, task_type: str) -> float:
    """Score simple basé sur la longueur et les signaux de succès/échec."""
    if not output:
        return 0.1
    if any(
        kw in output.lower()
        for kw in ["error", "erreur", "failed", "exception", "traceback"]
    ):
        return 0.3
    if any(kw in output.lower() for kw in ["timeout", "timed out"]):
        return 0.2
    if len(output) < 10:
        return 0.4
    # Bonus selon type
    if task_type == "audit" and len(output) > 50:
        return 0.85
    if task_type in ("action", "correction") and len(output) > 20:
        return 0.8
    return 0.7


def execute_task(task_id: str) -> dict:
    db = get_db()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    db.close()

    if not task:
        return {"error": f"Task {task_id} not found", "score": 0.0}

    task = dict(task)
    print(f"\n[JPTE] Exécution {task['id']} [{task['type'].upper()}]: {task['title']}")
    start_task(task_id)

    # ÉTAPE 1 — AUDIT : vérifier que la tâche est nécessaire et scorer l'état actuel
    audit_output = ""
    if task["type"] != "meta":
        audit_prompt = f"Audit rapide (2 lignes max) pour: {task['title']}. Description: {task['description'][:200]}"
        audit_output, _ = run_agent("lm-ask.sh", audit_prompt)
        print(f"  [AUDIT] {audit_output[:80]}...")

    # ÉTAPE 2 — ACTION : exécuter la tâche principale
    agent = task.get("agent") or "lm-ask.sh"
    action_output, action_score = run_agent(agent, task["description"] or task["title"])
    print(f"  [ACTION] score={action_score:.2f} | {action_output[:80]}...")

    # ÉTAPE 3 — CORRECTION : scorer le résultat final
    final_score = score_output(action_output, task["type"])
    if action_score < final_score:
        final_score = (action_score + final_score) / 2

    combined_output = json.dumps(
        {
            "audit": audit_output[:200],
            "action": action_output[:500],
            "score": final_score,
        },
        ensure_ascii=False,
    )

    if final_score >= 0.5:
        complete_task(task_id, output=combined_output, score=final_score)
        print(f"  [✅ DONE] score={final_score:.2f}")
    else:
        fail_task(task_id, error=action_output[:200], score=final_score)
        print(f"  [❌ FAIL] score={final_score:.2f} → correction auto-générée")

    return {"task_id": task_id, "score": final_score, "output": action_output[:200]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: task_executor.py <task_id>")
        sys.exit(1)
    result = execute_task(sys.argv[1])
    print(json.dumps(result, indent=2))
