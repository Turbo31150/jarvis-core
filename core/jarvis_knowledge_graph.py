#!/usr/bin/env python3
"""JARVIS Knowledge Graph — Map agent capabilities and skill relationships"""
import sqlite3, redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)
DB = "/home/turbo/jarvis/core/jarvis_master_index.db"

AGENT_CAPABILITIES = {
    "omega-trading-agent":  ["trading", "crypto", "signals", "backtesting", "market-analysis"],
    "omega-dev-agent":      ["code", "tdd", "refactor", "debug", "implementation"],
    "omega-analysis-agent": ["research", "comparison", "due-diligence", "analysis"],
    "omega-system-agent":   ["infra", "automation", "monitoring", "deployment"],
    "omega-security-agent": ["security", "audit", "incident", "hardening"],
    "jarvis-system-agent":  ["cluster", "gpu", "health-check", "mcp", "wave"],
    "ia-flow":              ["backpressure", "cpu-monitoring", "task-separation"],
}

def build_graph() -> dict:
    db = sqlite3.connect(DB)
    
    # Load agents from DB
    try:
        agents = db.execute("SELECT name, category, capabilities FROM agents LIMIT 50").fetchall()
    except:
        agents = []
    db.close()
    
    graph = {"nodes": [], "edges": [], "ts": datetime.now().isoformat()[:19]}
    
    # Add hardcoded agents
    for agent, caps in AGENT_CAPABILITIES.items():
        graph["nodes"].append({"id": agent, "type": "agent", "capabilities": caps})
        for cap in caps:
            graph["edges"].append({"from": agent, "to": cap, "type": "can_handle"})
    
    r.setex("jarvis:knowledge_graph", 3600, json.dumps(graph))
    return graph

def find_agent_for(task: str) -> list:
    raw = r.get("jarvis:knowledge_graph")
    if not raw:
        build_graph()
        raw = r.get("jarvis:knowledge_graph")
    graph = json.loads(raw)
    
    task_lower = task.lower()
    matches = []
    for node in graph["nodes"]:
        if node["type"] != "agent": continue
        caps = node.get("capabilities", [])
        score = sum(1 for c in caps if c in task_lower or any(w in c for w in task_lower.split()))
        if score > 0:
            matches.append({"agent": node["id"], "score": score, "capabilities": caps})
    return sorted(matches, key=lambda x: -x["score"])

if __name__ == "__main__":
    g = build_graph()
    print(f"Knowledge graph: {len(g['nodes'])} nodes, {len(g['edges'])} edges")
    matches = find_agent_for("analyze crypto trading signals")
    print("Best agents for 'analyze crypto trading signals':")
    for m in matches[:3]:
        print(f"  {m['agent']} (score={m['score']})")
