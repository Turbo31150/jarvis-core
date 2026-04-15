#!/usr/bin/env python3
"""
JPTE — Todolist Engine
SQLite auto-alimentée : CRUD tâches, sous-tâches, feedback loop, domino trigger.
Usage: python3 todolist_engine.py --test | --list | --session <id>
"""

import sqlite3
import uuid
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "todolist.db"


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    _init_schema(db)
    return db


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        original_request TEXT,
        status TEXT DEFAULT 'pending',
        total_tasks INTEGER DEFAULT 0,
        completed_tasks INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        parent_id TEXT,
        session_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        type TEXT DEFAULT 'action',
        status TEXT DEFAULT 'pending',
        priority INTEGER DEFAULT 2,
        complexity INTEGER DEFAULT 3,
        score_initial REAL,
        score_final REAL,
        agent TEXT,
        output TEXT,
        error TEXT,
        feedback TEXT,
        auto_generated INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id TEXT NOT NULL,
        depends_on TEXT NOT NULL,
        PRIMARY KEY (task_id, depends_on)
    );

    CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
    """)
    db.commit()


# ── Session ────────────────────────────────────────────────────────────────


def create_session(request: str) -> str:
    # C2 fix: full UUID pour éviter collisions sur sessions simultanées
    sid = str(uuid.uuid4())
    db = get_db()
    db.execute(
        "INSERT INTO sessions (id, original_request) VALUES (?, ?)",
        (sid, request),
    )
    db.commit()
    db.close()
    return sid


def update_session_status(session_id: str) -> None:
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM tasks WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    done = db.execute(
        "SELECT COUNT(*) FROM tasks WHERE session_id=? AND status='done'", (session_id,)
    ).fetchone()[0]
    status = "done" if done == total and total > 0 else "in_progress"
    completed_at = datetime.now().isoformat() if status == "done" else None
    db.execute(
        "UPDATE sessions SET total_tasks=?, completed_tasks=?, status=?, completed_at=? WHERE id=?",
        (total, done, status, completed_at, session_id),
    )
    db.commit()
    db.close()


# ── Tasks ──────────────────────────────────────────────────────────────────


def add_task(
    session_id: str,
    title: str,
    description: str = "",
    task_type: str = "action",
    priority: int = 2,
    complexity: int = 3,
    agent: str = "",
    parent_id: str | None = None,
    depends_on: list[str] | None = None,
    auto_generated: bool = False,
) -> str:
    # C2 fix: full UUID
    tid = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO tasks
           (id, parent_id, session_id, title, description, type, priority,
            complexity, agent, auto_generated)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            tid,
            parent_id,
            session_id,
            title,
            description,
            task_type,
            priority,
            complexity,
            agent,
            int(auto_generated),
        ),
    )
    if depends_on:
        for dep in depends_on:
            db.execute(
                "INSERT OR IGNORE INTO task_dependencies VALUES (?,?)", (tid, dep)
            )
    db.commit()
    db.close()
    return tid


def start_task(task_id: str) -> None:
    db = get_db()
    db.execute(
        "UPDATE tasks SET status='in_progress', started_at=? WHERE id=?",
        (datetime.now().isoformat(), task_id),
    )
    db.commit()
    db.close()


def complete_task(
    task_id: str,
    output: str = "",
    score: float = 1.0,
    feedback: str = "",
) -> None:
    db = get_db()
    db.execute(
        """UPDATE tasks SET status='done', score_final=?, output=?, feedback=?,
           completed_at=? WHERE id=?""",
        (score, output, feedback, datetime.now().isoformat(), task_id),
    )
    db.commit()
    # Auto-feed: if score < 0.5, generate correction subtask
    if score < 0.5:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            auto_feed_correction(dict(row), db)
    db.close()


def fail_task(task_id: str, error: str = "", score: float = 0.0) -> None:
    db = get_db()
    db.execute(
        """UPDATE tasks SET status='failed', score_final=?, error=?,
           completed_at=? WHERE id=?""",
        (score, error, datetime.now().isoformat(), task_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row:
        auto_feed_correction(dict(row), db)
    db.close()


def auto_feed_correction(task: dict, db: sqlite3.Connection) -> str:
    """Generate a correction subtask automatically when a task fails or scores low."""
    # C2 fix: full UUID
    tid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO tasks
           (id, parent_id, session_id, title, description, type, priority,
            complexity, agent, auto_generated)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (
            tid,
            task["id"],
            task["session_id"],
            f"[AUTO-CORRECTION] {task['title']}",
            f"Correction automatique suite échec/score bas de tâche {task['id']}. "
            f"Erreur: {task.get('error', 'score bas')}",
            "correction",
            1,  # P1 priority
            task.get("complexity", 3),
            task.get("agent", ""),
        ),
    )
    db.commit()
    return tid


# ── Queries ────────────────────────────────────────────────────────────────


def get_ready_tasks(session_id: str) -> list[dict]:
    """Tasks whose dependencies are all done."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM tasks WHERE session_id=? AND status='pending'", (session_id,)
        ).fetchall()
        ready = []
        for row in rows:
            deps = db.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id=?", (row["id"],)
            ).fetchall()
            if not deps:
                ready.append(dict(row))
                continue
            all_done = True
            for d in deps:
                dep_row = db.execute(
                    "SELECT status FROM tasks WHERE id=?", (d["depends_on"],)
                ).fetchone()
                if dep_row is None or dep_row["status"] != "done":
                    all_done = False
                    break
            if all_done:
                ready.append(dict(row))
        return ready
    finally:
        db.close()  # C3 fix: toujours fermé même si exception


def list_session(session_id: str) -> None:
    db = get_db()
    sess = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not sess:
        print(f"Session {session_id} introuvable")
        return
    print(f"\n=== SESSION {session_id} — {sess['status']} ===")
    print(f"Demande: {sess['original_request'][:80]}")
    print(f"Tâches: {sess['completed_tasks']}/{sess['total_tasks']}\n")
    tasks = db.execute(
        "SELECT * FROM tasks WHERE session_id=? ORDER BY priority, created_at",
        (session_id,),
    ).fetchall()
    for t in tasks:
        icon = {
            "done": "✅",
            "in_progress": "⏳",
            "failed": "❌",
            "pending": "⬜",
            "skipped": "⏭",
        }.get(t["status"], "?")
        auto = " [AUTO]" if t["auto_generated"] else ""
        score = f" score={t['score_final']:.2f}" if t["score_final"] is not None else ""
        print(f"  {icon} [{t['type'].upper()}] {t['title']}{auto}{score}")
        if t["error"]:
            print(f"     ⚠ {t['error'][:60]}")
    db.close()


def smoketest() -> None:
    print("=== JPTE Todolist Engine — Smoketest ===")
    sid = create_session("Test smoketest JPTE")
    print(f"Session créée: {sid}")

    t1 = add_task(sid, "Audit code", "Analyser main.py", "audit", agent="lm-ask.sh")
    t2 = add_task(
        sid,
        "Implémenter feature",
        "Coder X",
        "action",
        agent="omega-dev-agent",
        depends_on=[t1],
    )
    t3 = add_task(
        sid, "Valider résultat", "Tests + scoring", "correction", depends_on=[t2]
    )
    print(f"Tâches créées: {t1}, {t2}, {t3}")

    ready = get_ready_tasks(sid)
    print(f"Tâches prêtes: {[t['id'] for t in ready]}")

    start_task(t1)
    complete_task(t1, output="Code analysé OK", score=0.9)
    update_session_status(sid)

    ready = get_ready_tasks(sid)
    print(f"Tâches prêtes après T1: {[t['id'] for t in ready]}")

    # Test auto-feed
    start_task(t2)
    fail_task(t2, error="ImportError: module not found", score=0.0)
    update_session_status(sid)

    list_session(sid)
    print("\n✅ Smoketest OK")


if __name__ == "__main__":
    if "--test" in sys.argv:
        smoketest()
    elif "--list" in sys.argv and len(sys.argv) > 2:
        list_session(sys.argv[2])
    elif "--session" in sys.argv and len(sys.argv) > 2:
        list_session(sys.argv[2])
    else:
        print("Usage: todolist_engine.py --test | --list <session_id>")
