#!/bin/bash
# JARVIS Status — one-liner complet
python3 -c "
import redis, json
r = redis.Redis(decode_responses=True)
s = json.loads(r.get('jarvis:score') or '{}')
nodes = {n: r.get(f'jarvis:node:{n}:status') or '?' for n in ['M1','M2','M3']}
gpus = [r.get(f'jarvis:gpu:{i}:temp') or '?' for i in range(5)]
print(f\"Score: {s.get('total','?')}/100 | CPU:{s.get('cpu_temp','?')}°C | RAM:{s.get('ram_free_gb','?')}GB | GPUs:{'/'.join(gpus)}°C | M1:{nodes['M1']} M2:{nodes['M2']} M3:{nodes['M3']}\")
"
