#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Todolist Engine.
Provides CRUD and auto-feed logic for the todolist SQLite database.
"""
import sqlite3
import uuid
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data/todolist.db")

class TodolistEngine:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        # Already handled by setup script but ensuring here for robustness
        conn = sqlite3.connect(self.db_path)
        conn.close()

    def create_session(self, request):
        session_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO sessions (id, original_request, status, total_tasks, completed_tasks, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (session_id, request, 'in_progress', 0, 0, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return session_id

    def add_task(self, session_id, title, description, task_type='action', priority=2, complexity=3, agent=None, parent_id=None, dependencies=None):
        task_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT INTO tasks 
                     (id, parent_id, session_id, title, description, type, status, priority, complexity, agent, created_at) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (task_id, parent_id, session_id, title, description, task_type, 'pending', priority, complexity, agent, datetime.now().isoformat()))
        
        if dependencies:
            for dep_id in dependencies:
                c.execute("INSERT INTO task_dependencies (task_id, depends_on) VALUES (?, ?)", (task_id, dep_id))
        
        c.execute("UPDATE sessions SET total_tasks = total_tasks + 1 WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()
        return task_id

    def update_task_status(self, task_id, status, output=None, error=None, score=None):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        if status == 'in_progress':
            c.execute("UPDATE tasks SET status = ?, started_at = ? WHERE id = ?", (status, now, task_id))
        elif status in ['done', 'failed', 'skipped']:
            c.execute("""UPDATE tasks 
                         SET status = ?, output = ?, error = ?, score_final = ?, completed_at = ? 
                         WHERE id = ?""", 
                      (status, json.dumps(output) if output else None, error, score, now, task_id))
            
            # If done, update session count
            if status == 'done':
                c.execute("SELECT session_id FROM tasks WHERE id = ?", (task_id,))
                session_id = c.fetchone()[0]
                c.execute("UPDATE sessions SET completed_tasks = completed_tasks + 1 WHERE id = ?", (session_id,))
        
        conn.commit()
        conn.close()

    def get_pending_tasks(self, session_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""SELECT * FROM tasks 
                     WHERE session_id = ? AND status = 'pending' 
                     AND id NOT IN (SELECT task_id FROM task_dependencies WHERE depends_on IN (SELECT id FROM tasks WHERE status != 'done'))""", 
                  (session_id,))
        columns = [column[0] for column in c.description]
        tasks = [dict(zip(columns, row)) for row in c.fetchall()]
        conn.close()
        return tasks

    def get_all_tasks(self, session_id):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at", (session_id,))
        columns = [column[0] for column in c.description]
        tasks = [dict(zip(columns, row)) for row in c.fetchall()]
        conn.close()
        return tasks

if __name__ == "__main__":
    # Smoke test
    engine = TodolistEngine()
    sid = engine.create_session("Test request")
    print(f"Created session: {sid}")
    tid = engine.add_task(sid, "Test Task", "This is a test task")
    print(f"Added task: {tid}")
    engine.update_task_status(tid, 'in_progress')
    engine.update_task_status(tid, 'done', output={"result": "ok"}, score=0.9)
    tasks = engine.get_all_tasks(sid)
    print(f"All tasks in session: {json.dumps(tasks, indent=2)}")
