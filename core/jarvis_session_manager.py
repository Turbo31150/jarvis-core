#!/usr/bin/env python3
"""JARVIS Session Manager — Track Claude sessions, costs, achievements"""
import redis, json, time, sqlite3
from datetime import datetime

r = redis.Redis(decode_responses=True)
DB = "/home/turbo/jarvis/core/jarvis_master_index.db"

def start_session(session_id: str = None) -> str:
    if not session_id:
        session_id = f"session_{int(time.time())}"
    data = {"id": session_id, "start": datetime.now().isoformat()[:19],
            "actions": 0, "scripts_created": 0, "cost_usd": 0.0}
    r.setex(f"jarvis:session:{session_id}", 86400, json.dumps(data))
    r.set("jarvis:session:current", session_id)
    return session_id

def record_action(action_type: str, detail: str = ""):
    sid = r.get("jarvis:session:current")
    if not sid: return
    key = f"jarvis:session:{sid}"
    raw = r.get(key)
    if not raw: return
    data = json.loads(raw)
    data["actions"] += 1
    if action_type == "script_created":
        data["scripts_created"] += 1
    r.setex(key, 86400, json.dumps(data))
    r.lpush(f"jarvis:session:{sid}:log", json.dumps({"type": action_type, "detail": detail,
             "ts": datetime.now().isoformat()[:19]}))
    r.ltrim(f"jarvis:session:{sid}:log", 0, 199)

def get_current() -> dict:
    sid = r.get("jarvis:session:current")
    if not sid: return {}
    raw = r.get(f"jarvis:session:{sid}")
    return json.loads(raw) if raw else {}

def session_summary() -> dict:
    data = get_current()
    if not data: return {"error": "no active session"}
    
    # Count scripts this session
    db = sqlite3.connect(DB)
    today = datetime.now().strftime("%Y-%m-%d")
    today_scripts = db.execute(
        "SELECT COUNT(*) FROM scripts WHERE indexed_at LIKE ?", (today + "%",)
    ).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM scripts").fetchone()[0]
    db.close()
    
    elapsed = int(time.time()) - int(data["start"][:10].replace("-",""))
    return {**data, "scripts_today": today_scripts, "total_scripts": total}

if __name__ == "__main__":
    sid = start_session("session_20260414")
    record_action("startup", "session resumed")
    s = session_summary()
    print(f"Session: {s.get('id')} | Scripts today: {s.get('scripts_today')} | Total: {s.get('total_scripts')}")
