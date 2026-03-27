#!/usr/bin/env python3
"""JARVIS-COMET Dashboard Server — serves dashboard + API from jarvis-master.db."""

import json
import sqlite3
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "jarvis-master.db"
HTML = Path(__file__).resolve().parent.parent / "data" / "dashboard.html"


def query(sql, params=()):
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    con.close()
    return rows


def stats():
    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    total_eur = cur.execute("SELECT COALESCE(SUM(amount),0) FROM codeur_offers").fetchone()[0]
    offers = cur.execute("SELECT COUNT(*) FROM codeur_offers").fetchone()[0]
    runs = cur.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    actions = cur.execute("SELECT COUNT(*) FROM linkedin_actions").fetchone()[0]
    health = cur.execute(
        "SELECT m1_status, m2_status, m3_status, ol1_status FROM cluster_health ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    con.close()
    nodes_up = 0
    if health:
        nodes_up = sum(1 for s in health if s and s.lower() in ("online", "ok", "up"))
    return {
        "total_eur": total_eur, "offers": offers, "runs": runs,
        "actions": actions, "nodes_up": nodes_up, "nodes_total": 4,
    }


ROUTES = {
    "/api/offers": lambda: query("SELECT pid,title,amount,status,created_at FROM codeur_offers ORDER BY created_at DESC"),
    "/api/runs": lambda: query("SELECT id,run_type,question,valid_count,confidence,timestamp FROM workflow_runs ORDER BY timestamp DESC LIMIT 20"),
    "/api/actions": lambda: query("SELECT id,action_type,target_person,content,timestamp FROM linkedin_actions ORDER BY timestamp DESC LIMIT 20"),
    "/api/health": lambda: query("SELECT * FROM cluster_health ORDER BY timestamp DESC LIMIT 1"),
    "/api/stats": stats,
}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ROUTES:
            data = ROUTES[self.path]()
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/dashboard"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.read_bytes())
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"[dashboard] {args[0]}")


if __name__ == "__main__":
    print(f"JARVIS Dashboard → http://localhost:8888  (db: {DB})")
    HTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
