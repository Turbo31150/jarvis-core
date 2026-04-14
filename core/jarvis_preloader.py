#!/usr/bin/env python3
"""JARVIS Predictive Preloader — Anticipe prochaines requêtes LLM via patterns Redis"""
import redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

# Patterns de séquences observées
SEQUENCES = {
    "trading": ["market_scan", "signal_gen", "position_size", "order_place"],
    "code":    ["code_review", "test_gen", "debug", "deploy"],
    "content": ["research", "draft", "linkedin_post", "engage"],
}

def record_request(action: str):
    r.lpush("jarvis:request_history", action)
    r.ltrim("jarvis:request_history", 0, 99)

def predict_next(n=3) -> list:
    history = r.lrange("jarvis:request_history", 0, 9)
    if not history: return []
    last = history[0]
    predictions = []
    for seq_name, seq in SEQUENCES.items():
        if last in seq:
            idx = seq.index(last)
            for i in range(1, n+1):
                if idx+i < len(seq):
                    predictions.append({"action": seq[idx+i], "sequence": seq_name, "confidence": 1.0/(i)})
    return sorted(predictions, key=lambda x: -x["confidence"])[:n]

def warm_cache(predictions: list):
    """Pré-warm Redis cache pour les actions prédites"""
    for p in predictions:
        key = f"jarvis:preload:{p['action']}"
        if not r.exists(key):
            r.setex(key, 300, json.dumps({"status": "preloaded", "ts": datetime.now().isoformat()}))
    return len(predictions)

if __name__ == "__main__":
    # Test
    record_request("market_scan")
    preds = predict_next(3)
    print(f"Prédictions après 'market_scan': {preds}")
    warmed = warm_cache(preds)
    print(f"Cache pre-warm: {warmed} entrées")
    print("✅ Preloader OK")
