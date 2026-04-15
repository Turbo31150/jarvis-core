#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Feedback Collector.
Agrège résultats + génère nouvelles tâches.
"""
import sys
import json
import sqlite3
import os
from todolist_engine import TodolistEngine

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data/todolist.db")

def process_feedback(task_id):
    engine = TodolistEngine()
    
    # Get task results
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    columns = [column[0] for column in c.description]
    task = dict(zip(columns, c.fetchone()))
    conn.close()
    
    # If task failed or has low score, generate a correction task
    if task['status'] == 'failed' or (task['score_final'] and task['score_final'] < 0.5):
        print(f"[{task_id}] Feedback Loop: Task failed or low score ({task['score_final']}). Generating correction sub-task...")
        
        engine.add_task(
            session_id=task['session_id'],
            title=f"Corrective Action for: {task['title']}",
            description=f"Address failure in task {task_id}. Error: {task['error']}",
            task_type='correction',
            priority=task['priority'],
            complexity=2,
            agent='omega-system-agent',
            parent_id=task_id,
            # This task should be auto-generated
        )
        
        # Update original task feedback field
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tasks SET feedback = ? WHERE id = ?", ("Triggered corrective sub-task.", task_id))
        conn.commit()
        conn.close()
        
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: feedback_collector.py <task_id>")
        sys.exit(1)
        
    tid = sys.argv[1]
    process_feedback(tid)
