#!/usr/bin/env python3
"""JARVIS AIOps Engine — ML-driven anomaly detection + automated remediation"""
import redis, json, statistics, subprocess
from datetime import datetime

r = redis.Redis(decode_responses=True)

class AnomalyDetector:
    def __init__(self, window: int = 20, z_threshold: float = 2.5):
        self.window = window
        self.z_threshold = z_threshold
    
    def _zscore(self, history: list, current: float) -> float:
        if len(history) < 5:
            return 0.0
        mean = statistics.mean(history)
        std = statistics.stdev(history) if len(history) > 1 else 0
        return (current - mean) / std if std > 0 else 0.0
    
    def check_gpu_temps(self) -> list:
        anomalies = []
        for i in range(5):
            cur = r.get(f"jarvis:gpu:{i}:temp")
            if not cur:
                continue
            history = [float(t) for t in r.lrange(f"jarvis:gpu:{i}:temp_history", 0, self.window)]
            z = self._zscore(history, float(cur))
            if abs(z) > self.z_threshold:
                anomalies.append({
                    "type": "gpu_temp_zscore",
                    "gpu": i,
                    "current": float(cur),
                    "zscore": round(z, 2),
                    "severity": "critical" if abs(z) > 4 else "warning"
                })
        return anomalies
    
    def check_ram_trend(self) -> list:
        history = [float(x) for x in r.lrange("jarvis:ram:free_history", 0, self.window)]
        if len(history) < 5:
            return []
        cur = float(r.get("jarvis:ram:free_gb") or 999)
        z = self._zscore(history, cur)
        if z < -self.z_threshold:  # Unusually low RAM
            return [{"type": "ram_anomaly_zscore", "current_gb": cur,
                     "zscore": round(z, 2), "severity": "warning"}]
        return []
    
    def check_circuit_breakers(self) -> list:
        anomalies = []
        for key in r.scan_iter("jarvis:cb:*"):
            svc = key.replace("jarvis:cb:", "")
            state = r.hget(key, "state") or "closed"
            failures = int(r.hget(key, "failures") or 0)
            if state == "open" and failures > 5:
                anomalies.append({"type": "cb_persistent_open",
                                   "service": svc, "failures": failures,
                                   "severity": "high"})
        return anomalies

class AutoRemediation:
    def remediate(self, anomaly: dict) -> dict:
        atype = anomaly.get("type", "")
        action = "none"
        
        if atype == "gpu_temp_zscore" and anomaly.get("current", 0) > 80:
            # Power limit GPU
            try:
                gpu = anomaly["gpu"]
                subprocess.run(f"nvidia-smi -i {gpu} -pl 150", shell=True, timeout=5)
                action = f"reduced_power_limit_gpu{gpu}"
            except:
                action = "remediation_failed"
        
        elif atype == "ram_anomaly_zscore":
            # Drop caches
            try:
                subprocess.run("sync && echo 1 | sudo tee /proc/sys/vm/drop_caches",
                               shell=True, timeout=5)
                action = "dropped_caches"
            except:
                action = "remediation_failed"
        
        elif atype == "cb_persistent_open":
            # Reset circuit breaker
            svc = anomaly.get("service", "")
            r.delete(f"jarvis:cb:{svc}")
            action = f"reset_circuit_breaker_{svc}"
        
        return {**anomaly, "remediation": action, "ts": datetime.now().isoformat()[:19]}

def run_aiops() -> dict:
    detector = AnomalyDetector()
    remediator = AutoRemediation()
    
    all_anomalies = (detector.check_gpu_temps() +
                     detector.check_ram_trend() +
                     detector.check_circuit_breakers())
    
    remediations = []
    for anomaly in all_anomalies:
        result = remediator.remediate(anomaly)
        remediations.append(result)
        r.lpush("jarvis:aiops:log", json.dumps(result))
    r.ltrim("jarvis:aiops:log", 0, 99)
    
    summary = {"ts": datetime.now().isoformat()[:19],
               "anomalies": len(all_anomalies),
               "remediations": remediations}
    r.setex("jarvis:aiops:last", 300, json.dumps(summary))
    return summary

if __name__ == "__main__":
    result = run_aiops()
    print(f"AIOps: {result['anomalies']} anomalies detected")
    for rem in result["remediations"]:
        print(f"  {rem['type']} → {rem['remediation']}")
    if not result["remediations"]:
        print("  System healthy ✅")
