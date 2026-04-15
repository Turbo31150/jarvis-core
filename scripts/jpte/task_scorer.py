#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Task Scorer.
Scores complexity + priority + dependencies.
"""
import sys
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data/todolist.db")

def score_task(task_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT priority, complexity FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return 0
        
    priority, complexity = row
    # Lower priority (P1) is more important.
    # Higher complexity takes more weight.
    # Score formula: (3 - priority) * 0.4 + (complexity / 5.0) * 0.6
    score = (4 - priority) * 0.3 + (complexity / 5.0) * 0.7
    
    c.execute("UPDATE tasks SET score_initial = ? WHERE id = ?", (score, task_id))
    conn.commit()
    conn.close()
    return score

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: task_scorer.py <task_id>")
        sys.exit(1)
        
    tid = sys.argv[1]
    s = score_task(tid)
    print(f"Task {tid} scored: {s}")
