#!/usr/bin/env python3
"""JARVIS Auto-Triage — Classe les tâches pendantes par urgence + ressources dispo"""
import redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

URGENCY_KEYWORDS = {
    "critical": ["crash","down","fail","erreur critique","mort","broken","inaccessible"],
    "high":     ["lent","timeout","gpu","temperature","mémoire","saturé","alert"],
    "medium":   ["optimiser","améliorer","indexer","scanner","audit"],
    "low":      ["doc","readme","rapport","statistique","nightly"],
}

def score_urgency(task: str) -> tuple[str, int]:
    t = task.lower()
    for level, kws in URGENCY_KEYWORDS.items():
        if any(k in t for k in kws):
            return level, {"critical":100,"high":70,"medium":40,"low":10}[level]
    return "low", 10

def get_resources() -> dict:
    score_raw = r.get("jarvis:score")
    score = json.loads(score_raw) if score_raw else {}
    return {
        "cpu_ok":  score.get("cpu_thermal", 0) >= 20,
        "ram_ok":  score.get("ram", 0) >= 10,
        "gpu_ok":  score.get("gpu", 0) >= 20,
        "m2_up":   score.get("m2_up", False),
        "ol1_up":  score.get("ol1_up", False),
        "score":   score.get("total", 0),
    }

def triage(tasks: list[str]) -> list[dict]:
    res = get_resources()
    triaged = []
    for task in tasks:
        level, score = score_urgency(task)
        # Boost si ressources dispo
        if res["m2_up"]: score += 5
        if res["score"] >= 90: score += 5
        triaged.append({"task": task, "urgency": level, "score": score, "resources": res})
    return sorted(triaged, key=lambda x: -x["score"])

if __name__ == "__main__":
    # Exemple avec les tâches pendantes
    sample = [
        "M1 LMStudio down — restart needed",
        "Indexer 1177 docs Markdown",
        "GPU temperature monitor",
        "Rapport nightly hebdomadaire",
        "Audit services systemd",
        "Fix crash benchmark qwen9b",
    ]
    res = get_resources()
    print(f"Resources: score={res['score']}/100 M2={'✅' if res['m2_up'] else '❌'}")
    print("\nTriage:")
    for t in triage(sample):
        print(f"  [{t['urgency']:8}] {t['score']:3}pts  {t['task']}")
