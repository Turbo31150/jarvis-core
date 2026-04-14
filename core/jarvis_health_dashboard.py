#!/usr/bin/env python3
"""JARVIS Health Dashboard — Terminal-based health overview combining all subsystems"""

import redis
import json
import time
import os

r = redis.Redis(decode_responses=True)


def _color(text: str, code: str) -> str:
    if not os.isatty(1):
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t):
    return _color(t, "32")


def yellow(t):
    return _color(t, "33")


def red(t):
    return _color(t, "31")


def bold(t):
    return _color(t, "1")


def _status_color(status: str) -> str:
    if status in ("ok", "up", "green", "closed", "running", "active"):
        return green(status)
    elif status in ("warn", "degraded", "half_open", "yellow"):
        return yellow(status)
    return red(status)


def render() -> str:
    lines = []
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(bold(f"=== JARVIS HEALTH DASHBOARD === {ts} ==="))

    # Score
    score_raw = r.get("jarvis:score")
    score = json.loads(score_raw) if score_raw else {}
    total = score.get("total", 0)
    score_col = (
        green(f"{total}/100")
        if total >= 85
        else (yellow(f"{total}/100") if total >= 65 else red(f"{total}/100"))
    )
    lines.append(
        f"  Score: {score_col}  CPU:{score.get('cpu_thermal', 0)}/20  RAM:{score.get('ram', 0)}/20  GPU:{score.get('gpu', 0)}/20  LLM:{score.get('llm', 0)}/20  Svc:{score.get('services', 0)}/20"
    )

    # GPUs
    gpu_parts = []
    for i in range(5):
        temp = r.get(f"jarvis:gpu:{i}:temp") or "?"
        vram = r.get(f"jarvis:gpu:{i}:vram_pct") or "?"
        try:
            t = float(temp)
            tc = (
                green(f"{t:.0f}C")
                if t < 70
                else (yellow(f"{t:.0f}C") if t < 82 else red(f"{t:.0f}C"))
            )
        except Exception:
            tc = str(temp)
        gpu_parts.append(f"GPU{i}:{tc}/{vram}%")
    lines.append("  GPUs: " + "  ".join(gpu_parts))

    # Nodes
    node_parts = []
    for node in ["M1", "M2", "M32", "OL1"]:
        status = r.get(f"jarvis:node:{node}:status") or "?"
        node_parts.append(f"{node}:{_status_color(status)}")
    lines.append("  Nodes: " + "  ".join(node_parts))

    # Circuit breakers
    cb_parts = []
    for key in r.scan_iter("jarvis:cb:*"):
        svc = key.replace("jarvis:cb:", "")
        state = r.hget(key, "state") or "closed"
        cb_parts.append(f"{svc}:{_status_color(state)}")
    if cb_parts:
        lines.append("  CB: " + "  ".join(cb_parts))

    # LLM Router stats
    llm_parts = []
    for task_type in ["code", "fast", "default", "reasoning"]:
        cnt = r.get(f"jarvis:llm_router:{task_type}:count") or "0"
        err = r.get(f"jarvis:llm_router:{task_type}:errors") or "0"
        lat = r.get(f"jarvis:llm_router:{task_type}:last_ms") or "?"
        if int(cnt) > 0:
            llm_parts.append(f"{task_type}:{cnt}req/{err}err/{lat}ms")
    if llm_parts:
        lines.append("  LLM: " + "  ".join(llm_parts))

    # Health scorer
    try:
        from jarvis_health_scorer import get

        hs = get()
        hs_score = hs.get("score", 0)
        hs_status = hs.get("status", "?")
        hs_col = (
            green(str(hs_score))
            if hs_score >= 80
            else (yellow(str(hs_score)) if hs_score >= 55 else red(str(hs_score)))
        )
        lines.append(f"  Health scorer: {hs_col}/100 [{_status_color(hs_status)}]")
    except Exception:
        pass

    # Cluster state
    try:
        from jarvis_cluster_state import get_state

        cs = get_state()
        lines.append(
            f"  Cluster state: {_status_color(cs['state'])}  reason: {cs.get('reason', '')[:40]}"
        )
    except Exception:
        pass

    # Recent alerts
    try:
        alerts = r.lrange("jarvis:alert_log", 0, 2)
        if alerts:
            lines.append("  Recent alerts:")
            for a in alerts:
                data = json.loads(a)
                sev = data.get("severity", "info")
                col = (
                    yellow
                    if sev == "warn"
                    else (red if sev in ("error", "critical") else green)
                )
                lines.append(
                    f"    {col('*')} [{sev}] {data.get('source', '?')}: {data.get('message', '')[:60]}"
                )
    except Exception:
        pass

    # Self-test last result
    last_test = r.get("jarvis:self_test:last")
    if last_test:
        lt = json.loads(last_test)
        pct = lt.get("score_pct", 0)
        pct_col = (
            green(f"{pct}%")
            if pct >= 80
            else (yellow(f"{pct}%") if pct >= 60 else red(f"{pct}%"))
        )
        lines.append(
            f"  Self-test: {lt.get('passed', 0)}/{lt.get('passed', 0) + lt.get('failed', 0)} modules {pct_col} [{lt.get('ts', '')}]"
        )

    lines.append(bold("=" * 55))
    return "\n".join(lines)


def watch(interval: int = 5):
    """Live dashboard — refresh every N seconds using ANSI cursor reset."""
    try:
        first = True
        while True:
            output = render()
            lines = output.count("\n") + 1
            if not first:
                # Move cursor up N lines
                print(f"\033[{lines}A", end="")
            print(output)
            first = False
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def stats() -> dict:
    return {"dashboard": "ok", "ts": time.time()}


if __name__ == "__main__":
    print(render())
