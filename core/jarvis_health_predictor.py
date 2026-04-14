#!/usr/bin/env python3
"""JARVIS Health Predictor — Predict issues before they happen using trend analysis"""
import redis, json, statistics
from datetime import datetime

r = redis.Redis(decode_responses=True)

def predict_gpu_thermal() -> list:
    alerts = []
    for i in range(5):
        history = [float(t) for t in r.lrange(f"jarvis:gpu:{i}:temp_history", 0, 9) if t]
        if len(history) >= 4:
            # Linear regression slope
            n = len(history)
            x = list(range(n))
            x_mean = sum(x) / n
            y_mean = sum(history) / n
            slope = sum((x[j]-x_mean)*(history[j]-y_mean) for j in range(n)) / \
                    max(sum((x[j]-x_mean)**2 for j in range(n)), 0.001)
            if slope > 1.5:  # >1.5°C per sample (each 30s) = rising fast
                alerts.append({
                    "type": "gpu_thermal_trend",
                    "gpu": i,
                    "slope_per_sample": round(slope, 2),
                    "current": history[0],
                    "eta_85c": round((85 - history[0]) / slope * 30 / 60, 1),  # minutes
                    "severity": "warning"
                })
    return alerts

def predict_ram_exhaustion() -> list:
    history = [float(x) for x in r.lrange("jarvis:ram:free_history", 0, 9) if x]
    if len(history) < 4:
        return []
    n = len(history)
    slope = (history[0] - history[-1]) / max(n - 1, 1)  # GB/sample (each 30s)
    if slope > 0.2:  # losing >200MB per sample
        current = history[0]
        eta_minutes = round(current / slope * 0.5, 1) if slope > 0 else 999
        return [{"type": "ram_exhaustion_trend", "free_gb": current,
                 "drain_gb_per_min": round(slope * 2, 2), "eta_minutes": eta_minutes,
                 "severity": "warning" if eta_minutes > 30 else "critical"}]
    return []

def run_predictions() -> dict:
    preds = {"ts": datetime.now().isoformat()[:19], "alerts": []}
    preds["alerts"] += predict_gpu_thermal()
    preds["alerts"] += predict_ram_exhaustion()
    r.setex("jarvis:predictions", 120, json.dumps(preds))
    for alert in preds["alerts"]:
        r.publish("jarvis:events", json.dumps({**alert, "ts": preds["ts"]}))
    return preds

if __name__ == "__main__":
    preds = run_predictions()
    print(f"Predictions: {len(preds['alerts'])} alerts")
    for a in preds["alerts"]:
        print(f"  {a['type']}: {a}")
