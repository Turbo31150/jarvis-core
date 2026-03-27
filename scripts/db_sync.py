#!/usr/bin/env python3
"""JARVIS DB Sync — Synchronize all data into jarvis-master.db"""
import sqlite3, json, time, os, re, requests, subprocess
from pathlib import Path

DB = Path("/home/turbo/IA/Core/jarvis/data/jarvis-master.db")
DATA = Path("/home/turbo/IA/Core/jarvis/data")

def get_conn():
    return sqlite3.connect(str(DB))

def sync_cluster_health():
    """Snapshot cluster state."""
    conn = get_conn()
    c = conn.cursor()
    cluster = {}
    for name, host, port in [("m1","127.0.0.1",1234),("m2","192.168.1.26",1234),("m3","192.168.1.113",1234)]:
        try:
            r = requests.get(f"http://{host}:{port}/v1/models", timeout=3)
            cluster[name] = {"s": "ONLINE", "m": len(r.json()["data"])}
        except:
            cluster[name] = {"s": "OFFLINE", "m": 0}

    gpu = subprocess.run(["nvidia-smi","--query-gpu=temperature.gpu","--format=csv,noheader,nounits"],
        capture_output=True, text=True).stdout.strip().replace('\n','/')
    vram = subprocess.run(["nvidia-smi","--query-gpu=memory.used","--format=csv,noheader,nounits"],
        capture_output=True, text=True).stdout.strip().replace('\n','/')

    c.execute("INSERT OR REPLACE INTO cluster_health VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.strftime("%Y-%m-%dT%H:%M:%S"),
         cluster["m1"]["s"], cluster["m1"]["m"],
         cluster["m2"]["s"], cluster["m2"]["m"],
         cluster["m3"]["s"], cluster["m3"]["m"],
         "ONLINE", gpu, vram))
    conn.commit()
    conn.close()
    return cluster

def sync_codeur_scan(projects, applied_pids):
    """Save scanned projects."""
    conn = get_conn()
    c = conn.cursor()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for p in projects:
        kw = ",".join(p.get("kw", []))
        c.execute("INSERT OR IGNORE INTO codeur_projects VALUES (?,?,?,?,?,?,?,?)",
            (p.get("pid",""), p.get("title",""), p.get("budget",""),
             p.get("offers",0), kw, p.get("score",0), ts,
             1 if p.get("pid","") in applied_pids else 0))
    conn.commit()
    conn.close()

def sync_workflow_run(run_type, question, results, valid_count=0, confidence=0):
    """Save a workflow run."""
    conn = get_conn()
    c = conn.cursor()
    sources = json.dumps(list(results.keys())[:10])
    results_json = json.dumps(results, ensure_ascii=False)[:3000]
    c.execute("INSERT INTO workflow_runs (run_type,question,sources,results,valid_count,confidence,timestamp) VALUES (?,?,?,?,?,?,?)",
        (run_type, question[:200], sources, results_json, valid_count, confidence, time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()

def sync_linkedin_action(action_type, target, content, url=""):
    """Save a LinkedIn action."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO linkedin_actions (action_type,target_person,content,post_url,timestamp) VALUES (?,?,?,?,?)",
        (action_type, target, content[:500], url, time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()

def sync_codeur_offer(pid, title, amount, duration=14, status="deposee", client=None, url=""):
    """Save or update a Codeur offer."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO codeur_offers
        (pid,title,amount,duration,status,client_name,project_url,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM codeur_offers WHERE pid=?),?),?)""",
        (pid, title, amount, duration, status, client, url, pid,
         time.strftime("%Y-%m-%dT%H:%M:%S"), time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()

def get_stats():
    """Get dashboard stats."""
    conn = get_conn()
    c = conn.cursor()
    stats = {}
    stats["offers"] = c.execute("SELECT COUNT(*), SUM(amount) FROM codeur_offers").fetchone()
    stats["actions"] = c.execute("SELECT COUNT(*) FROM linkedin_actions").fetchone()[0]
    stats["runs"] = c.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    stats["scans"] = c.execute("SELECT COUNT(DISTINCT scanned_at) FROM codeur_projects").fetchone()[0]
    conn.close()
    return stats

def dashboard():
    """Print dashboard."""
    s = get_stats()
    print(f"  Offres: {s['offers'][0]} ({s['offers'][1]}EUR)")
    print(f"  LinkedIn actions: {s['actions']}")
    print(f"  Workflow runs: {s['runs']}")
    print(f"  Codeur scans: {s['scans']}")

if __name__ == "__main__":
    print("JARVIS DB Sync")
    sync_cluster_health()
    print("  Cluster: synced")
    dashboard()
