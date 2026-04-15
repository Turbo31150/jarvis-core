#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Compiler.
Merge final multi-tâches → output cohérent.
"""
import sys
import json
import sqlite3
import os
from todolist_engine import TodolistEngine

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data/todolist.db")

def compile_results(session_id):
    engine = TodolistEngine()
    tasks = engine.get_all_tasks(session_id)
    
    # Check if session request exists
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT original_request FROM sessions WHERE id = ?", (session_id,))
    row = c.fetchone()
    request = row[0] if row else "Unknown"
    conn.close()
    
    summary = {
        "session_id": session_id,
        "original_request": request,
        "total_tasks": len(tasks),
        "results": []
    }
    
    for t in tasks:
        if t['status'] == 'done':
            try:
                output = json.loads(t['output']) if t['output'] else {}
                summary['results'].append({
                    "task": t['title'],
                    "status": "success",
                    "action_data": output.get('action', {}).get('action_output', "No data")
                })
            except Exception:
                summary['results'].append({"task": t['title'], "status": "parse_error"})
        else:
            summary['results'].append({
                "task": t['title'],
                "status": t['status'],
                "error": t['error']
            })
            
    # Final merge into a string
    final_text = f"JARVIS JPTE Execution Report for: {request}\n"
    final_text += "="*50 + "\n"
    for res in summary['results']:
        final_text += f"- [{res['status'].upper()}] {res['task']}: {res.get('action_data', res.get('error', 'N/A'))}\n"
        
    return final_text

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: compiler.py <session_id>")
        sys.exit(1)
        
    sid = sys.argv[1]
    report = compile_results(sid)
    print(report)
