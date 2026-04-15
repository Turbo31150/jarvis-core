#!/usr/bin/env python3
"""JARVIS Parallel Task Engine (JPTE) - Task Decomposer.
Decomposes a user request into a JSON list of tasks.
Uses the provided TODOLIST_ENGINE for storage.
"""
import sys
import json
from todolist_engine import TodolistEngine

def decompose(session_id, request):
    """Mock decomposer - in real usage, this should call an LLM.
    We'll implement a simple rule-based one for now or just a few tasks.
    """
    engine = TodolistEngine()
    
    # Example tasks for a generic request
    tasks = [
        {
            "title": "Initial Audit",
            "description": f"Audit existing resources for request: {request}",
            "type": "audit",
            "priority": 1,
            "complexity": 2,
            "agent": "omega-analysis-agent"
        },
        {
            "title": "Primary Action",
            "description": f"Perform the main task action: {request}",
            "type": "action",
            "priority": 2,
            "complexity": 4,
            "agent": "omega-dev-agent"
        },
        {
            "title": "Final Correction & Validation",
            "description": f"Validate the results of the primary action and apply fixes.",
            "type": "correction",
            "priority": 1,
            "complexity": 3,
            "agent": "omega-system-agent"
        }
    ]
    
    task_ids = []
    for t in tasks:
        # Map 'type' key to 'task_type' argument
        params = t.copy()
        if 'type' in params:
            params['task_type'] = params.pop('type')
        tid = engine.add_task(session_id, **params)
        task_ids.append(tid)
        
    return task_ids

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: task_decomposer.py <session_id> <request>")
        sys.exit(1)
        
    sid = sys.argv[1]
    req = sys.argv[2]
    tids = decompose(sid, req)
    print(f"Decomposed request into {len(tids)} tasks: {tids}")
