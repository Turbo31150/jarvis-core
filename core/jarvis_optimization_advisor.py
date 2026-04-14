#!/usr/bin/env python3
"""JARVIS Optimization Advisor — Recommend optimizations based on current metrics"""
import redis, json
from datetime import datetime

r = redis.Redis(decode_responses=True)

def analyze() -> list:
    recommendations = []
    
    # GPU utilization check
    max_temp = 0
    max_vram = 0
    for i in range(5):
        temp = float(r.get(f"jarvis:gpu:{i}:temp") or 0)
        vram = float(r.get(f"jarvis:gpu:{i}:vram_pct") or 0)
        max_temp = max(max_temp, temp)
        max_vram = max(max_vram, vram)
    
    if max_temp < 40:
        recommendations.append({
            "category": "gpu",
            "priority": "low",
            "action": "GPUs underutilized thermally — consider loading more models",
            "metric": f"max_temp={max_temp}°C"
        })
    
    if max_vram < 10:
        recommendations.append({
            "category": "vram",
            "priority": "low",
            "action": "VRAM mostly free — can load larger models for better quality",
            "metric": f"max_vram={max_vram}%"
        })
    
    # RAM check
    ram_free = float(r.get("jarvis:ram:free_gb") or 99)
    if ram_free > 30:
        recommendations.append({
            "category": "ram",
            "priority": "low",
            "action": f"RAM abundant ({ram_free:.0f}GB free) — ZRAM can be reduced or disabled",
            "metric": f"ram_free={ram_free:.0f}GB"
        })
    
    # LLM router usage
    total_calls = sum(int(r.get(f"jarvis:llm_router:{t}:count") or 0)
                      for t in ["fast","code","trading","summary","default","classify"])
    if total_calls == 0:
        recommendations.append({
            "category": "llm",
            "priority": "medium",
            "action": "LLM router has 0 calls — integrate into more workflows",
            "metric": "calls=0"
        })
    
    # Cache hit ratio
    hits = int(r.get("jarvis:ctx:hits") or 0)
    misses = int(r.get("jarvis:ctx:misses") or 0)
    if misses > 10 and hits / max(hits+misses, 1) < 0.2:
        recommendations.append({
            "category": "cache",
            "priority": "medium",
            "action": "Context cache hit rate low — consider semantic_dedup threshold tuning",
            "metric": f"hit_ratio={hits/(hits+misses):.0%}"
        })
    
    # Score check
    score_raw = r.get("jarvis:score")
    if score_raw:
        score = json.loads(score_raw)
        if score.get("total", 100) < 80:
            recommendations.append({
                "category": "system",
                "priority": "high",
                "action": f"System score below 80 — investigate component scores",
                "metric": f"score={score.get('total')}"
            })
    
    result = {"ts": datetime.now().isoformat()[:19], "recommendations": recommendations}
    r.setex("jarvis:advisor", 3600, json.dumps(result))
    return recommendations

if __name__ == "__main__":
    recs = analyze()
    print(f"Optimization recommendations: {len(recs)}")
    for rec in recs:
        pri = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(rec["priority"], "⚪")
        print(f"  {pri} [{rec['category']}] {rec['action']} ({rec['metric']})")
