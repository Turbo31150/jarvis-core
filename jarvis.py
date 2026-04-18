#!/usr/bin/env python3
"""JARVIS — Main entry point. Run workflows, agents, and system checks."""
import sys, os, json, time, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def cmd_health():
    from core.workflows import morning_startup
    print(json.dumps(morning_startup(), indent=2, default=str))

def cmd_incidents():
    from core.workflows import incident_triage
    incidents = incident_triage()
    if not incidents:
        print("✅ No incidents")
    else:
        for i in incidents:
            print(f"  [{i['severity']:8}] {i['type']}: {i.get('name','')}")

def cmd_eod():
    from core.workflows import end_of_day
    print(json.dumps(end_of_day(), indent=2))

def cmd_network():
    from core.network.health import full_report
    print(json.dumps(full_report(), indent=2, default=str))

def cmd_audit():
    from core.github_audit import daily_summary
    print(json.dumps(daily_summary(), indent=2))

def cmd_tasks():
    import sqlite3
    conn = sqlite3.connect("data/jarvis-master.db")
    c = conn.cursor()
    done = c.execute("SELECT COUNT(*) FROM task_queue WHERE status='completed'").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM task_queue WHERE status='pending'").fetchone()[0]
    print(f"Tasks: {done} completed, {pending} pending")
    print("\nPending:")
    for row in c.execute("SELECT category, title, priority FROM task_queue WHERE status='pending' ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"):
        print(f"  [{row[2]:8}] [{row[0]:12}] {row[1]}")
    conn.close()

def cmd_query(prompt):
    from core.router.dispatcher import TaskDispatcher
    from core.tasks.models import TaskRequest
    req = TaskRequest(prompt=prompt, task_type="fast")
    disp = TaskDispatcher()
    result = disp.dispatch(req)
    print(f"[{result.node}] ({result.duration:.1f}s)")
    print(result.output)

def cmd_dashboard():
    cmd_health()
    print()
    cmd_incidents()
    print()
    cmd_tasks()

def cmd_openclaw(args=None):
    from core.openclaw_bridge import build_openclaw_bridge
    bridge = build_openclaw_bridge()
    if not args:
        print(f"OpenClaw Health: {'✅ OK' if bridge.is_healthy() else '❌ DOWN'}")
        print("\nAgents:")
        for a in bridge.list_agents():
            print(a.get("raw", "n/a"))
    elif args[0] == "query" and len(args) > 1:
        res = bridge.run_agent(" ".join(args[1:]))
        if res["success"]:
            print(res["output"])
        else:
            print(f"Error: {res['error']}")
    else:
        print("Usage: jarvis openclaw [query <prompt>]")

COMMANDS = {
    "health": cmd_health,
    "incidents": cmd_incidents,
    "eod": cmd_eod,
    "network": cmd_network,
    "audit": cmd_audit,
    "tasks": cmd_tasks,
    "dashboard": cmd_dashboard,
    "openclaw": cmd_openclaw,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JARVIS CLI")
    parser.add_argument("command", nargs="?", default="dashboard", choices=list(COMMANDS.keys()) + ["query"],
        help="Command to run")
    parser.add_argument("args", nargs="*", help="Additional arguments")
    args = parser.parse_args()

    if args.command == "query" and args.args:
        cmd_query(" ".join(args.args))
    elif args.command == "openclaw":
        cmd_openclaw(args.args)
    elif args.command in COMMANDS:
        COMMANDS[args.command]()
    else:
        parser.print_help()

