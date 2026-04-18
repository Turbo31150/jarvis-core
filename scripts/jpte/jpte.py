#!/usr/bin/env python3
"""
JPTE — JARVIS Parallel Task Engine — Point d'entrée principal
Pipeline complet : Décomposition → Scoring → Dispatch parallèle → Compilation

Usage:
  python3 jpte.py "ma demande complète"
  python3 jpte.py --session <sid>   # reprendre une session existante
  python3 jpte.py --list <sid>      # afficher l'état d'une session
"""

import sys
import asyncio
from task_decomposer import decompose
from task_dispatcher import dispatch as dispatch_session
from compiler import compile_results as compile_session
from todolist_engine import list_session, update_session_status


async def run_async(request: str, session_id: str | None = None) -> dict:
    print(f"\n{'=' * 60}")
    print("JPTE — JARVIS PARALLEL TASK ENGINE")
    print(f"{'=' * 60}")
    print(f"Demande : {request[:80]}")

    # Phase 1 — Décomposition
    print("\n[1/3] Décomposition en tâches parallèles...")
    sid, task_ids = decompose(request, session_id)
    print(f"  → Session {sid} | {len(task_ids)} tâches créées: {task_ids}")

    # Phase 2 — Dispatch parallèle (I2 fix: await au lieu de asyncio.run imbriqué)
    print("\n[2/3] Dispatch parallèle (AUDIT → ACTION → CORRECTION)...")
    results = await dispatch_session(sid)
    update_session_status(sid)

    # Phase 3 — Compilation finale
    print("\n[3/3] Compilation finale...")
    compiled = compile_session(sid)

    return compiled


def run(request: str, session_id: str | None = None) -> dict:
    return asyncio.run(run_async(request, session_id))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if "--list" in sys.argv and len(sys.argv) > 2:
        list_session(sys.argv[2])
    elif "--session" in sys.argv and len(sys.argv) > 2:
        sid = sys.argv[sys.argv.index("--session") + 1]
        asyncio.run(dispatch_session(sid))
        compile_session(sid)
    else:
        request = " ".join(sys.argv[1:])
        result = run(request)
        print(f"\n✅ JPTE terminé — Score: {result.get('avg_score', 0)}")
