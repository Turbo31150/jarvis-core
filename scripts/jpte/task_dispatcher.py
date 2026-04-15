#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Task Dispatcher.
Lances agents/skills/CLI en parallèle.
"""
import sys
import asyncio
import os
import subprocess
from todolist_engine import TodolistEngine

def run_task_executor(task_id):
    """Sync wrapper to run task executor in a separate process."""
    try:
        # Resolve the absolute path to task_executor.py
        executor_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_executor.py")
        result = subprocess.run([sys.executable, executor_path, task_id], capture_output=True, text=True)
        return result.stdout
    except Exception as e:
        return str(e)

async def dispatch(session_id):
    engine = TodolistEngine()
    
    while True:
        pending_tasks = engine.get_pending_tasks(session_id)
        if not pending_tasks:
            # Check if session is actually done
            tasks = engine.get_all_tasks(session_id)
            if all(t['status'] in ['done', 'failed', 'skipped'] for t in tasks):
                print(f"Session {session_id} complete.")
                break
            else:
                # Wait for running tasks
                print("Waiting for running tasks...")
                await asyncio.sleep(2)
                continue
        
        # Dispatch each pending task that has no outstanding dependencies
        print(f"Dispatching {len(pending_tasks)} tasks...")
        tasks_to_run = []
        for task in pending_tasks:
            engine.update_task_status(task['id'], 'in_progress')
            tasks_to_run.append(task['id'])
            
        # Run executors in parallel
        loop = asyncio.get_event_loop()
        futures = [loop.run_in_executor(None, run_task_executor, tid) for tid in tasks_to_run]
        results = await asyncio.gather(*futures)
        
        for i, res in enumerate(results):
            print(f"Task {tasks_to_run[i]} executor finished: {res}")
            
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: task_dispatcher.py <session_id>")
        sys.exit(1)
        
    sid = sys.argv[1]
    asyncio.run(dispatch(sid))
