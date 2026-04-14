#!/usr/bin/env python3
"""
JPTE — Compiler
Merge tous les outputs d'une session en résultat cohérent.
Met à jour la mémoire projet et commit git si code modifié.
Usage: python3 compiler.py <session_id>
"""

import json
import subprocess
import sys
from datetime import datetime
from todolist_engine import get_db

MEMORY_PATH = "/home/turbo/.claude/projects/-home-turbo-IA-Core-jarvis/memory/"


def compile_session(session_id: str) -> dict:
    db = get_db()
    sess = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        return {"error": f"Session {session_id} introuvable"}

    tasks = db.execute(
        "SELECT * FROM tasks WHERE session_id=? ORDER BY priority, created_at",
        (session_id,),
    ).fetchall()

    done_tasks = [dict(t) for t in tasks if t["status"] == "done"]
    failed_tasks = [dict(t) for t in tasks if t["status"] == "failed"]
    auto_tasks = [dict(t) for t in tasks if t["auto_generated"]]

    outputs = []
    for t in done_tasks:
        if t.get("output"):
            try:
                o = json.loads(t["output"])
                if o.get("action"):
                    outputs.append(
                        f"[{t['type'].upper()}] {t['title']}: {o['action'][:200]}"
                    )
            except Exception:
                outputs.append(
                    f"[{t['type'].upper()}] {t['title']}: {str(t['output'])[:200]}"
                )

    scores = [t.get("score_final", 0) or 0 for t in done_tasks]
    avg_score = sum(scores) / len(scores) if scores else 0

    result = {
        "session_id": session_id,
        "request": sess["original_request"][:100],
        "status": sess["status"],
        "tasks_done": len(done_tasks),
        "tasks_failed": len(failed_tasks),
        "auto_corrections": len(auto_tasks),
        "avg_score": round(avg_score, 2),
        "outputs": outputs,
        "compiled_at": datetime.now().isoformat(),
    }

    db.close()

    # Print rapport
    print(f"\n{'=' * 60}")
    print(f"JPTE COMPILATION — Session {session_id}")
    print(f"{'=' * 60}")
    print(f"Demande : {result['request']}")
    print(f"Statut  : {result['status']} | Score moyen : {result['avg_score']}")
    print(
        f"Tâches  : {result['tasks_done']} done / {result['tasks_failed']} failed / {result['auto_corrections']} auto-corrections"
    )
    print("\nOutputs compilés :")
    for o in outputs:
        print(f"  • {o[:120]}")
    print(f"{'=' * 60}")

    # Auto-commit si tâches meta effectuées
    _auto_commit(session_id, result)

    return result


def _auto_commit(session_id: str, result: dict) -> None:
    """Commit automatique si des fichiers ont été modifiés."""
    try:
        git_status = subprocess.run(
            ["git", "-C", "/home/turbo/IA/Core/jarvis", "status", "--short"],
            capture_output=True,
            text=True,
        )
        if git_status.stdout.strip():
            print("\n[JPTE] Fichiers modifiés détectés — auto-commit...")
            subprocess.run(
                [
                    "git",
                    "-C",
                    "/home/turbo/IA/Core/jarvis",
                    "add",
                    "scripts/jpte/",
                    "data/todolist.db",
                ],
                capture_output=True,
            )
            msg = f"feat(jpte): session {session_id} — {result['tasks_done']} tasks done, score {result['avg_score']}"
            subprocess.run(
                ["git", "-C", "/home/turbo/IA/Core/jarvis", "commit", "-m", msg],
                capture_output=True,
            )
            print(f"[JPTE] Commit effectué: {msg}")
    except Exception as e:
        print(f"[JPTE] Auto-commit skipped: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: compiler.py <session_id>")
        sys.exit(1)
    result = compile_session(sys.argv[1])
    print(json.dumps(result, indent=2))
