#!/usr/bin/env python3
"""
JPTE — Task Dispatcher
Lance les tâches prêtes en parallèle via asyncio.gather.
Gère les dépendances, le domino trigger, et l'auto-feed.
Usage: python3 task_dispatcher.py <session_id>
"""

import asyncio
import json
import sys
from todolist_engine import get_ready_tasks, update_session_status, get_db, list_session

MAX_PARALLEL = 5


async def run_task_async(task_id: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "/home/turbo/IA/Core/jarvis/scripts/jpte/task_executor.py",
        task_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        proc.kill()
        return {"task_id": task_id, "score": 0.0, "output": "TIMEOUT"}

    output = stdout.decode().strip()
    try:
        result = json.loads(output.split("\n")[-1])
    except Exception:
        result = {"task_id": task_id, "score": 0.5, "output": output[-200:]}
    return result


async def dispatch_session(session_id: str) -> list[dict]:
    all_results = []

    for round_num in range(20):
        ready = get_ready_tasks(session_id)
        if not ready:
            db = get_db()
            pending = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE session_id=? AND status='pending'",
                (session_id,),
            ).fetchone()[0]
            db.close()
            if pending == 0:
                print(f"\n[JPTE] Session {session_id} — toutes les tâches terminées")
                break
            await asyncio.sleep(1)
            continue

        batch = ready[:MAX_PARALLEL]
        print(
            f"\n[JPTE] Round {round_num + 1} — {len(batch)} tâche(s): {[t['id'] for t in batch]}"
        )

        results = await asyncio.gather(
            *[run_task_async(t["id"]) for t in batch],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                all_results.append({"error": str(r), "score": 0.0})
            else:
                all_results.append(r)

        update_session_status(session_id)

    return all_results


def main(session_id: str) -> list[dict]:
    print(f"[JPTE Dispatcher] Session: {session_id}")
    results = asyncio.run(dispatch_session(session_id))
    scores = [r.get("score", 0) for r in results if isinstance(r, dict)]
    avg = sum(scores) / len(scores) if scores else 0
    print(f"[JPTE] {len(results)} tâches | Score moyen: {avg:.2f}")
    list_session(session_id)
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: task_dispatcher.py <session_id>")
        sys.exit(1)
    main(sys.argv[1])
