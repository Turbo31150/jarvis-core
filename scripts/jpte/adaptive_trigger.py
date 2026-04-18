#!/usr/bin/env python3
"""
JARVIS Adaptive Trigger Engine
Surveille les fichiers TODO/mémoire, score le flux système, déclenche les pipelines.
Usage: python3 adaptive_trigger.py [--once|--watch|--status]

Architecture:
  scan_keywords() → score_flux() → decide_pipeline() → dispatch_to_openclaw()
  Domino: score > 0.8 → tâche suivante automatique
"""

import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

BASE_DIR = Path("/home/turbo/IA/Core/jarvis")
WATCH_INTERVAL = 300  # 5 minutes

WATCH_FILES = [
    BASE_DIR / "memory" / "TODO_ACTIONS.md",
    BASE_DIR / "memory" / "MEMORY.md",
    Path("/home/turbo/.claude/projects/-home-turbo-IA-Core-jarvis/memory/MEMORY.md"),
]

LOG_DIR = BASE_DIR / "reports" / "adaptive-trigger"
COOLDOWN_FILE = BASE_DIR / "data" / "adaptive_trigger_cooldowns.json"

# Cooldown par pipeline (secondes)
COOLDOWNS = {
    "codeur": 10800,  # 3h
    "hackathon": 21600,  # 6h
    "cluster": 7200,  # 2h
    "jpte": 3600,  # 1h
}

# Mots-clés → pipeline
KEYWORD_MAP = {
    "codeur": [
        "codeur",
        "mission",
        "freelance",
        "appel d'offre",
        "ao",
        "client",
        "devis",
    ],
    "hackathon": [
        "hackathon",
        "lablab",
        "devpost",
        "submission",
        "milan",
        "elevenlabs",
        "mvp",
    ],
    "cluster": [
        "cluster",
        "gpu",
        "vram",
        "m1",
        "m2",
        "m3",
        "ol1",
        "node",
        "noeud",
        "health",
    ],
    "jpte": ["jpte", "todolist", "tâche", "pipeline", "dispatcher", "task", "executor"],
}

# Agents OpenClaw par pipeline (noms réels dans openclaw agents list)
OPENCLAW_AGENTS = {
    "codeur": "jarvis-task-balancer",
    "hackathon": "omega-analysis-agent",
    "cluster": "jarvis-cluster-health",
    "jpte": "task-decomposer-prime",
}

# Agent maître — point d'entrée unique pour orchestration complète
MASTER_AGENT = "openclaw-master"

# Prompts par pipeline
PIPELINE_PROMPTS = {
    "codeur": "Scan Codeur.com for new missions matching JARVIS competencies. Report new AOs found.",
    "hackathon": "Check hackathon deadlines for Milan AI Olympics and ElevenLabs. Report status.",
    "cluster": "Run full cluster health check on M1/M2/OL1. Report GPU VRAM and service status.",
    "jpte": "Run JPTE autofeed: process pending correction tasks and update session statuses.",
}


# ── Cooldown management ───────────────────────────────────────────────────────


def load_cooldowns() -> dict:
    if COOLDOWN_FILE.exists():
        try:
            return json.loads(COOLDOWN_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cooldowns(data: dict) -> None:
    COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOLDOWN_FILE.write_text(json.dumps(data, indent=2))


def is_on_cooldown(pipeline: str) -> bool:
    data = load_cooldowns()
    last_run = data.get(pipeline, 0)
    elapsed = time.time() - last_run
    return elapsed < COOLDOWNS.get(pipeline, 3600)


def mark_pipeline_run(pipeline: str) -> None:
    data = load_cooldowns()
    data[pipeline] = time.time()
    save_cooldowns(data)


# ── Flux scoring ──────────────────────────────────────────────────────────────


def score_system_flux() -> float:
    """Score 0-1 based on GPU/RAM availability. Higher = more resources free."""
    score = 1.0
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
            usages = []
            for line in lines:
                parts = line.split(",")
                if len(parts) == 2:
                    used, total = int(parts[0].strip()), int(parts[1].strip())
                    if total > 0:
                        usages.append(used / total)
            if usages:
                avg_usage = sum(usages) / len(usages)
                score -= avg_usage * 0.4  # GPU weight: 40%
    except Exception:
        score -= 0.1  # penalize slightly if nvidia-smi unavailable

    try:
        mem_info = Path("/proc/meminfo").read_text()
        mem_total = mem_free = 0
        for line in mem_info.split("\n"):
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_free = int(line.split()[1])
        if mem_total > 0:
            usage = 1.0 - (mem_free / mem_total)
            score -= usage * 0.3  # RAM weight: 30%
    except Exception:
        score -= 0.05

    return max(0.0, min(1.0, score))


# ── Keyword scanning ──────────────────────────────────────────────────────────


def scan_keywords_in_file(file_path: Path) -> dict[str, int]:
    """Returns {pipeline: keyword_count} for each pipeline."""
    counts: dict[str, int] = {p: 0 for p in KEYWORD_MAP}
    if not file_path.exists():
        return counts
    try:
        text = file_path.read_text(errors="ignore").lower()
        for pipeline, keywords in KEYWORD_MAP.items():
            for kw in keywords:
                counts[pipeline] += text.count(kw)
    except Exception:
        pass
    return counts


def aggregate_keyword_scores() -> dict[str, float]:
    """Aggregate across all watch files, normalize to 0-1."""
    totals: dict[str, int] = {p: 0 for p in KEYWORD_MAP}
    for watch_file in WATCH_FILES:
        counts = scan_keywords_in_file(watch_file)
        for pipeline, count in counts.items():
            totals[pipeline] += count

    # Normalize: cap at 10 occurrences = score 1.0
    return {p: min(1.0, c / 10.0) for p, c in totals.items()}


# ── Decision engine ───────────────────────────────────────────────────────────


def decide_pipelines(flux_score: float, keyword_scores: dict[str, float]) -> list[dict]:
    """Returns list of {pipeline, score, reason} sorted by score desc."""
    decisions = []
    for pipeline, kw_score in keyword_scores.items():
        if is_on_cooldown(pipeline):
            continue
        # Combined score: 60% keywords + 40% flux
        combined = kw_score * 0.6 + flux_score * 0.4
        if combined >= 0.25:  # threshold: at least 25%
            decisions.append(
                {
                    "pipeline": pipeline,
                    "score": combined,
                    "kw_score": kw_score,
                    "flux_score": flux_score,
                    "reason": f"kw={kw_score:.2f} flux={flux_score:.2f}",
                }
            )
    decisions.sort(key=lambda x: x["score"], reverse=True)
    return decisions


# ── Dispatch ──────────────────────────────────────────────────────────────────


def dispatch_via_lm_ask(pipeline: str, prompt: str) -> str:
    """Fallback dispatch using lm-ask.sh."""
    try:
        result = subprocess.run(
            ["bash", "/home/turbo/jarvis/scripts/lm-ask.sh", prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
        return result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] lm-ask.sh exceeded 180s for pipeline={pipeline}"
    except Exception as exc:
        return f"[ERROR] lm-ask.sh: {exc}"


def dispatch_via_jpte(pipeline: str) -> str:
    """Dispatch via JPTE task engine."""
    jpte_script = BASE_DIR / "scripts" / "jpte" / "jpte.py"
    if not jpte_script.exists():
        # Try finding it in current dir if not in BASE_DIR
        jpte_script = Path(__file__).parent / "jpte.py"
    if not jpte_script.exists():
        return "[ERROR] jpte.py not found"
    try:
        request = f"[ADAPTIVE-TRIGGER] Auto-pipeline: {pipeline} — {PIPELINE_PROMPTS.get(pipeline, '')}"
        result = subprocess.run(
            [sys.executable, str(jpte_script), request],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] JPTE exceeded 300s for pipeline={pipeline}"
    except Exception as exc:
        return f"[ERROR] JPTE: {exc}"


def run_pipeline(pipeline: str, score: float) -> str:
    """Route pipeline to OpenClaw agent or JPTE fallback."""
    agent = OPENCLAW_AGENTS.get(pipeline, "default")
    prompt = PIPELINE_PROMPTS.get(pipeline, f"Auto-trigger for pipeline: {pipeline}")

    output = ""

    # Dispatch via Agent Maître — orchestre tout le cluster stratégique
    try:
        master_msg = (
            f"[ADAPTIVE-TRIGGER] pipeline={pipeline} score={score:.2f} | {prompt}"
        )
        master_result = subprocess.run(
            ["openclaw", "agent", "--agent", MASTER_AGENT, "--message", master_msg],
            capture_output=True,
            text=True,
            timeout=60,
        )
        master_out = master_result.stdout.strip() or master_result.stderr.strip()
        if master_out and master_result.returncode == 0:
            return f"[MASTER] {master_out[:300]}"
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    # Fallback direct — agent spécialisé du pipeline
    try:
        direct_result = subprocess.run(
            ["openclaw", "agent", "--agent", agent, "--message", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
        direct_out = direct_result.stdout.strip() or direct_result.stderr.strip()
        if direct_out and direct_result.returncode == 0:
            return f"[OpenClaw:{agent}] {direct_out[:300]}"
    except Exception:
        pass

    # Try lm-ask.sh next
    output = dispatch_via_lm_ask(pipeline, prompt)
    if output and "ERROR" not in output:
        return f"[lm-ask] {output[:300]}"

    # Final fallback: JPTE
    return dispatch_via_jpte(pipeline)


# ── Domino cascade ────────────────────────────────────────────────────────────

DOMINO_CHAIN = ["cluster", "jpte", "codeur", "hackathon"]


def trigger_domino(current_pipeline: str, score: float) -> list[str]:
    """If score > 0.8, return next pipelines in domino chain."""
    if score < 0.8:
        return []
    try:
        idx = DOMINO_CHAIN.index(current_pipeline)
        return [DOMINO_CHAIN[idx + 1]] if idx + 1 < len(DOMINO_CHAIN) else []
    except ValueError:
        return []


# ── Logging ───────────────────────────────────────────────────────────────────


def log_cycle(decisions: list[dict], results: list[dict], flux_score: float) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.md"

    timestamp = datetime.now().strftime("%H:%M:%S")
    lines = [
        f"\n## {timestamp} — flux={flux_score:.2f}",
        f"**Pipelines évalués**: {len(decisions)}",
    ]
    for r in results:
        status = "✅" if r.get("ok") else "⚠️"
        lines.append(
            f"- {status} `{r['pipeline']}` score={r['score']:.2f} → {r['output'][:80]}"
        )
    if not results:
        lines.append("- ⬜ Aucun pipeline déclenché (seuil non atteint ou cooldown)")

    with open(log_file, "a") as f:
        f.write("\n".join(lines) + "\n")


# ── Main loop ─────────────────────────────────────────────────────────────────


async def run_once() -> list[dict]:
    flux_score = score_system_flux()
    kw_scores = aggregate_keyword_scores()
    decisions = decide_pipelines(flux_score, kw_scores)

    results = []
    domino_queue = list(decisions)
    triggered = set()

    while domino_queue:
        item = domino_queue.pop(0)
        pipeline = item["pipeline"]
        if pipeline in triggered:
            continue

        print(f"[ADAPTIVE] → {pipeline} (score={item['score']:.2f}, {item['reason']})")
        output = run_pipeline(pipeline, item["score"])
        mark_pipeline_run(pipeline)
        triggered.add(pipeline)

        ok = "ERROR" not in output and "TIMEOUT" not in output
        results.append(
            {"pipeline": pipeline, "score": item["score"], "output": output, "ok": ok}
        )

        # Domino cascade
        if ok:
            for next_pipeline in trigger_domino(pipeline, item["score"]):
                if next_pipeline not in triggered and not is_on_cooldown(next_pipeline):
                    domino_queue.append(
                        {
                            "pipeline": next_pipeline,
                            "score": item["score"] * 0.9,
                            "reason": "domino",
                        }
                    )

    log_cycle(decisions, results, flux_score)
    return results


async def watch_loop() -> None:
    print(f"[ADAPTIVE] Watch mode actif — cycle={WATCH_INTERVAL}s")
    while True:
        try:
            results = await run_once()
            for r in results:
                status = "✅" if r.get("ok") else "❌"
                print(f"  {status} {r['pipeline']}: {r['output'][:80]}")
        except Exception as exc:
            print(f"[ADAPTIVE][ERROR] {exc}", file=sys.stderr)
        await asyncio.sleep(WATCH_INTERVAL)


def show_status() -> None:
    flux_score = score_system_flux()
    kw_scores = aggregate_keyword_scores()
    cooldowns = load_cooldowns()
    now = time.time()

    print(f"[ADAPTIVE STATUS] flux={flux_score:.2f}")
    print(f"{'Pipeline':<12} {'KW Score':<10} {'Cooldown':<12} {'Combined':<10}")
    print("-" * 46)
    for pipeline in KEYWORD_MAP:
        kw = kw_scores.get(pipeline, 0.0)
        combined = kw * 0.6 + flux_score * 0.4
        last = cooldowns.get(pipeline, 0)
        remaining = max(0, COOLDOWNS[pipeline] - (now - last))
        cd_str = f"{int(remaining)}s" if remaining > 0 else "ready"
        print(f"{pipeline:<12} {kw:<10.2f} {cd_str:<12} {combined:<10.2f}")


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--once"
    if mode == "--watch":
        asyncio.run(watch_loop())
    elif mode == "--status":
        show_status()
    else:
        results = asyncio.run(run_once())
        print(json.dumps(results, indent=2, ensure_ascii=False))
