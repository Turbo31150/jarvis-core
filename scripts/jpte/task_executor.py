#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Task Executor.
Exécute AUDIT→ACTION→CORRECTION par tâche.
"""
import sys
import json
import time
import sqlite3
import os
from todolist_engine import TodolistEngine

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data/todolist.db")

def execute_audit(task):
    print(f"[{task['id']}] Step 1: AUDIT - Analyzing existing state...")
    # Simulation: audit existing files or logs
    time.sleep(1)
    return {"status": "success", "audit_score": 0.85, "observations": "Initial state verified."}

def execute_action(task, audit_data):
    print(f"[{task['id']}] Step 2: ACTION - Executing assigned agent {task['agent']}...")
    # Simulation: run the actual agent or CLI
    time.sleep(2)
    return {"status": "success", "action_output": f"Executed action for {task['title']}"}

def execute_correction(task, action_data):
    print(f"[{task['id']}] Step 3: CORRECTION - Validating results...")
    # Simulation: verify files, run tests, etc.
    time.sleep(1)
    # 90% success rate simulation
    import random
    if random.random() > 0.1:
        return {"status": "success", "final_score": 0.95, "feedback": "Task completed successfully."}
    else:
        return {"status": "failed", "final_score": 0.4, "feedback": "Correction detected inconsistencies."}

def run_task(task_id):
    engine = TodolistEngine()
    
    # Get full task details
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    columns = [column[0] for column in c.description]
    task = dict(zip(columns, c.fetchone()))
    conn.close()
    
    try:
        # Step 1: AUDIT
        audit_res = execute_audit(task)
        
        # Step 2: ACTION
        action_res = execute_action(task, audit_res)
        
        # Step 3: CORRECTION
        correction_res = execute_correction(task, action_res)
        
        # Aggregate results
        final_output = {
            "audit": audit_res,
            "action": action_res,
            "correction": correction_res
        }
        
        status = 'done' if correction_res['status'] == 'success' else 'failed'
        engine.update_task_status(task_id, status, output=final_output, score=correction_res.get('final_score', 0.5))
        
        # Trigger feedback collector (new sub-tasks if needed)
        feedback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback_collector.py")
        import subprocess
        subprocess.run([sys.executable, feedback_path, task_id])
        
        return f"Task {task_id} {status}"
        
    except Exception as e:
        engine.update_task_status(task_id, 'failed', error=str(e))
        return f"Task {task_id} failed with error: {e}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: task_executor.py <task_id>")
        sys.exit(1)
        
    tid = sys.argv[1]
    res = run_task(tid)
    print(res)
