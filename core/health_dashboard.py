#!/usr/bin/env python3
"""JARVIS Health Dashboard — Score /100 + état cluster en temps réel"""
import redis, json, time, os
from datetime import datetime

r = redis.Redis(decode_responses=True)

RESET="\033[0m"; BOLD="\033[1m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"; CYAN="\033[96m"; DIM="\033[2m"

def color_score(v, max_v=20):
    pct = v / max_v
    if pct >= 0.9: return GREEN
    if pct >= 0.5: return YELLOW
    return RED

def color_temp(t):
    if t < 70: return GREEN
    if t < 82: return YELLOW
    return RED

def render():
    score_raw = r.get("jarvis:score")
    score = json.loads(score_raw) if score_raw else {}
    total = score.get("total", "?")
    tc = GREEN if total == 100 else (YELLOW if isinstance(total,int) and total >= 70 else RED)

    print(f"\033[H\033[J", end="")  # clear screen
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║  JARVIS Health Dashboard  {DIM}{datetime.now().strftime('%H:%M:%S')}{RESET}{BOLD}{CYAN}            ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════╝{RESET}")

    # Score global
    bar_len = int((total if isinstance(total,int) else 0) / 2)
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"\n  Score global: {tc}{BOLD}{total}/100{RESET}  [{tc}{bar}{RESET}]\n")

    # Catégories
    cats = [
        ("CPU Thermal", score.get("cpu_thermal",0), f"{score.get('cpu_temp','?')}°C"),
        ("RAM libre",   score.get("ram",0),           f"{score.get('ram_free_gb','?')} GB"),
        ("GPU temp",    score.get("gpu",0),            f"max {score.get('gpu_max_temp','?')}°C"),
        ("LLM backends",score.get("llm",0),            f"M2={'✅' if score.get('m2_up') else '❌'} OL1={'✅' if score.get('ol1_up') else '❌'}"),
        ("Services",    score.get("services",0),       "health+redis+telegram"),
    ]
    for name, val, detail in cats:
        c = color_score(val)
        print(f"  {c}{'█'*val}{'░'*(20-val)}{RESET}  {val:2}/20  {name:15} {DIM}{detail}{RESET}")

    # GPU détail
    print(f"\n  {BOLD}GPUs:{RESET}")
    for i in range(5):
        t = r.get(f"jarvis:gpu:{i}:temp")
        v = r.get(f"jarvis:gpu:{i}:vram_pct")
        p = r.get(f"jarvis:gpu:{i}:power")
        if t:
            tc2 = color_temp(float(t))
            print(f"    GPU{i}: {tc2}{t}°C{RESET}  VRAM {v}%  {p}W")

    # Nodes
    print(f"\n  {BOLD}Nodes:{RESET}")
    for node in ["M1","M2","M3"]:
        s = r.get(f"jarvis:node:{node}:status") or "unknown"
        c = GREEN if s=="up" else RED
        print(f"    {node}: {c}{s}{RESET}")

    # LLM
    print(f"\n  {BOLD}LLM:{RESET}")
    for key in r.scan_iter("jarvis:llm:m*"):
        d = json.loads(r.get(key) or "{}")
        ok_c = GREEN if d.get("ok") else RED
        name = key.replace("jarvis:llm:","")
        print(f"    {ok_c}{'✅' if d.get('ok') else '❌'}{RESET} {name:12} {d.get('latency_ms','?')}ms  {d.get('tokens_s','?')}tok/s")

    print(f"\n  {DIM}Rafraîchi: {score.get('ts','?')}  Redis keys: {r.dbsize()}{RESET}")

if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        render()
    else:
        print("\033[?25l", end="")  # hide cursor
        try:
            while True:
                render()
                time.sleep(5)
        except KeyboardInterrupt:
            print("\033[?25h")  # restore cursor
