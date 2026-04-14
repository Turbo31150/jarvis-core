#!/usr/bin/env python3
"""JARVIS Prompt Optimizer — Améliore les prompts selon score historique"""
import redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

PROMPT_TEMPLATES = {
    "code_review":    "Analyse ce code Python et liste les bugs critiques uniquement:\n{code}",
    "summarize":      "Résume en 3 points clés maximum:\n{text}",
    "dispatch":       "Tâche: {task}\nAgent optimal parmi: {agents}\nRéponds juste le nom de l'agent.",
    "trading_signal": "Données: {data}\nSignal BUY/SELL/HOLD uniquement avec confiance 0-100.",
    "debug":          "Erreur: {error}\nCause probable + fix en 2 lignes max.",
}

def get_best_prompt(template_name: str) -> str:
    # Cherche version optimisée dans Redis
    opt = r.get(f"jarvis:prompt_opt:{template_name}")
    if opt:
        return json.loads(opt)["prompt"]
    return PROMPT_TEMPLATES.get(template_name, "")

def record_prompt_result(template_name: str, prompt: str, score: float):
    key = f"jarvis:prompt_history:{template_name}"
    r.lpush(key, json.dumps({"prompt": prompt, "score": score, "ts": datetime.now().isoformat()}))
    r.ltrim(key, 0, 49)
    # Si score > seuil, sauvegarder comme meilleur
    best = r.get(f"jarvis:prompt_opt:{template_name}")
    best_score = json.loads(best)["score"] if best else 0
    if score > best_score:
        r.set(f"jarvis:prompt_opt:{template_name}", json.dumps({"prompt": prompt, "score": score}))

def stats():
    result = {}
    for name in PROMPT_TEMPLATES:
        history = r.lrange(f"jarvis:prompt_history:{name}", 0, -1)
        if history:
            scores = [json.loads(h)["score"] for h in history]
            result[name] = {"uses": len(scores), "avg_score": round(sum(scores)/len(scores),1)}
    return result

if __name__ == "__main__":
    print("Prompt templates:", list(PROMPT_TEMPLATES.keys()))
    record_prompt_result("debug", PROMPT_TEMPLATES["debug"], 8.5)
    record_prompt_result("summarize", PROMPT_TEMPLATES["summarize"], 9.0)
    print("Stats:", stats())
    print("✅ Prompt optimizer OK")
