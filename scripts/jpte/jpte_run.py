#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Master Runner.
Orchestrates the whole pipeline for a user request.
"""
import sys
import asyncio
import os
import subprocess
from todolist_engine import TodolistEngine

async def run_pipeline(request):
    engine = TodolistEngine()
    
    # 1. Create Session
    sid = engine.create_session(request)
    print(f"[*] Created Session: {sid}")
    
    # 2. Decompose
    print(f"[*] Decomposing request...")
    subprocess.run([sys.executable, "task_decomposer.py", sid, request])
    
    # 3. Score
    tasks = engine.get_all_tasks(sid)
    print(f"[*] Scoring {len(tasks)} tasks...")
    for t in tasks:
        subprocess.run([sys.executable, "task_scorer.py", t['id']])
        
    # 4. Dispatch (Exécution parallèle)
    print(f"[*] Starting Parallel Execution...")
    # Import dispatcher and run it
    from task_dispatcher import dispatch
    await dispatch(sid)
    
    # 5. Compile Final Results
    print(f"[*] Compiling final report...")
    report_res = subprocess.run([sys.executable, "compiler.py", sid], capture_output=True, text=True)
    print("\n" + report_res.stdout)
    
    return sid

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: jpte_run.py <request>")
        sys.exit(1)
        
    req = " ".join(sys.argv[1:])
    asyncio.run(run_pipeline(req))
