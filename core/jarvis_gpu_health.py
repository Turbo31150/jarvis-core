#!/usr/bin/env python3
"""JARVIS GPU Health — Active GPU liveness checks + driver diagnostics"""
import subprocess, redis, json, time
from datetime import datetime

r = redis.Redis(decode_responses=True)

def check_gpu_liveness() -> list:
    """Active check: query each GPU individually, detect hangs"""
    results = []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,pcie.link.gen.current,pcie.link.width.current,"
             "ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total,"
             "retired_pages.single_bit_ecc.count,power.draw,clocks.gr,clocks.mem",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10
        )
        for line in out.stdout.strip().split("\n"):
            if not line.strip(): continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 10: continue
            idx = int(parts[0])
            name = parts[1]
            pcie_gen = parts[2]
            pcie_width = parts[3]
            ecc_corr = int(parts[4]) if parts[4].isdigit() else 0
            ecc_uncorr = int(parts[5]) if parts[5].isdigit() else 0
            
            health = {"gpu": idx, "name": name, "pcie": f"Gen{pcie_gen}x{pcie_width}",
                      "ecc_corrected": ecc_corr, "ecc_uncorrected": ecc_uncorr,
                      "alive": True, "issues": []}
            
            if ecc_uncorr > 0:
                health["issues"].append(f"ECC_UNCORRECTED:{ecc_uncorr}")
                health["alive"] = False
            if pcie_gen == "1" and idx == 0:  # RTX2060 should be Gen2+
                health["issues"].append(f"PCIE_DEGRADED:Gen{pcie_gen}x{pcie_width}")
            
            r.setex(f"jarvis:gpu:{idx}:health", 300, json.dumps(health))
            if not health["alive"] or health["issues"]:
                r.publish("jarvis:events", json.dumps({
                    "type": "gpu_health_issue", "data": health,
                    "severity": "critical" if not health["alive"] else "warning",
                    "ts": datetime.now().isoformat()[:19]
                }))
            results.append(health)
    except Exception as e:
        results.append({"error": str(e)})
    return results

def get_driver_info() -> dict:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=driver_version",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=5)
        line = out.stdout.strip().split("\n")[0]
        parts = [p.strip() for p in line.split(",")]
        return {"driver": parts[0], "cuda": parts[1] if len(parts) > 1 else "?"}
    except:
        return {"driver": "?", "cuda": "?"}

if __name__ == "__main__":
    driver = get_driver_info()
    print(f"Driver: {driver['driver']} | CUDA: {driver['cuda']}")
    results = check_gpu_liveness()
    for g in results:
        if "error" in g:
            print(f"  ERROR: {g['error']}")
        else:
            status = "✅" if g["alive"] and not g["issues"] else "⚠️"
            issues = ", ".join(g["issues"]) if g["issues"] else "ok"
            print(f"  GPU{g['gpu']}: {status} {g['name'][:20]} PCIe={g['pcie']} {issues}")
