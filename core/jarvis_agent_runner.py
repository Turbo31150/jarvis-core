#!/usr/bin/env python3
"""JARVIS Agent Runner — Autonomous agent loop with tools, memory, and goals"""

import redis
import json
import time
import uuid
from datetime import datetime

r = redis.Redis(decode_responses=True)
PREFIX = "jarvis:agents"

MAX_ITERATIONS = 5


def run_agent(
    goal: str,
    agent_id: str = None,
    tools: list = None,
    max_iter: int = MAX_ITERATIONS,
) -> dict:
    aid = agent_id or f"agent_{uuid.uuid4().hex[:8]}"
    tools_available = tools or ["jarvis_score", "redis_get", "bash", "infer"]
    history = []
    t_start = time.time()

    # Register agent
    r.hset(f"{PREFIX}:{aid}", mapping={
        "goal": goal[:200],
        "status": "running",
        "started_at": datetime.now().isoformat()[:19],
        "iterations": 0,
    })
    r.sadd(f"{PREFIX}:active", aid)

    for iteration in range(max_iter):
        r.hincrby(f"{PREFIX}:{aid}", "iterations", 1)

        # Build prompt with history
        history_str = "\n".join([f"Step {i+1}: {h['action']} → {str(h['result'])[:80]}" for i, h in enumerate(history)])
        prompt = f"""Goal: {goal}
Available tools: {', '.join(tools_available)}
History: {history_str if history_str else 'none'}

What should I do next? Reply with:
ACTION: <tool_name>
PARAMS: <json params>
REASONING: <why>

Or if goal is achieved:
DONE: <final answer>"""

        try:
            from jarvis_inference_gateway import infer
            res = infer(prompt, task_type="fast", use_cache=False)
            response = res.get("response", "")
        except Exception as e:
            response = f"DONE: Error calling LLM: {str(e)}"

        # Parse response
        if "DONE:" in response:
            final_answer = response.split("DONE:")[-1].strip()
            history.append({"action": "DONE", "result": final_answer})
            break

        action_match = None
        for line in response.split("\n"):
            if line.startswith("ACTION:"):
                action_match = line.replace("ACTION:", "").strip()
            elif line.startswith("PARAMS:") and action_match:
                try:
                    params = json.loads(line.replace("PARAMS:", "").strip())
                except Exception:
                    params = {}
                # Execute tool
                try:
                    from jarvis_tool_executor import execute
                    tool_result = execute(action_match, params)
                    history.append({"action": action_match, "params": params, "result": tool_result.get("result", "error")})
                except Exception as e:
                    history.append({"action": action_match, "result": str(e)})
                action_match = None
        else:
            if not action_match:
                history.append({"action": "noop", "result": "no valid action parsed"})

    duration_s = round(time.time() - t_start, 1)
    final = history[-1]["result"] if history else "no result"

    r.hset(f"{PREFIX}:{aid}", mapping={
        "status": "done",
        "iterations": len(history),
        "duration_s": duration_s,
        "completed_at": datetime.now().isoformat()[:19],
    })
    r.srem(f"{PREFIX}:active", aid)
    r.sadd(f"{PREFIX}:completed", aid)

    result = {
        "agent_id": aid,
        "goal": goal[:100],
        "iterations": len(history),
        "duration_s": duration_s,
        "final_answer": str(final)[:200],
        "history": history,
    }
    r.lpush(f"{PREFIX}:results", json.dumps(result))
    r.ltrim(f"{PREFIX}:results", 0, 49)
    return result


def active_agents() -> list:
    return list(r.smembers(f"{PREFIX}:active"))


def stats() -> dict:
    active = r.scard(f"{PREFIX}:active")
    completed = r.scard(f"{PREFIX}:completed")
    return {"active": active, "completed": completed}


if __name__ == "__main__":
    print("Running agent: check system health...")
    res = run_agent("Check the JARVIS system score and report if it's above 90", max_iter=2)
    print(f"  Agent {res['agent_id']}: {res['iterations']} iterations, {res['duration_s']}s")
    print(f"  Final: {res['final_answer'][:100]}")
    print(f"Stats: {stats()}")
