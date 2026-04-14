#!/bin/bash
# Diagnostic OpenClaw — état cluster + agents actifs

echo "=== CLUSTER STATUS ==="
M1=$(curl -s --connect-timeout 2 http://192.168.1.85:1234/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); loaded=[m['id'] for m in d if [m["id"] for m in d["data"]][0]+" (+"+str(len(d["data"])-1)+" autres)")"
M2=$(curl -s --connect-timeout 2 http://192.168.1.26:1234/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'M2 ✓ {len(d[\"data\"])} models')" 2>/dev/null || echo "M2 ✗ offline")
M3=$(curl -s --connect-timeout 2 http://192.168.1.133:1234/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'M3 ✓ {len(d[\"data\"])} models')" 2>/dev/null || echo "M3 ✗ offline")
OL1=$(curl -s --connect-timeout 2 http://127.0.0.1:11434/api/tags 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'OL1 ✓ {len(d[\"models\"])} models')" 2>/dev/null || echo "OL1 ✗ offline")
echo "  $M1"
echo "  $M2"
echo "  $M3"
echo "  $OL1"

echo ""
echo "=== OPENCLAW ==="
GW=$(curl -s http://127.0.0.1:18789/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('Gateway ✓' if d.get('ok') else 'Gateway ✗')" 2>/dev/null || echo "Gateway ✗")
echo "  $GW"
echo "  $(openclaw agents list 2>/dev/null | grep -c 'Model:') agents configurés"
echo "  Dernières sessions:"
ls -lt ~/.openclaw/agents/*/sessions/*.jsonl 2>/dev/null | head -5 | awk '{print "   ", $NF}' | sed "s|/home/turbo/.openclaw/agents/||" | sed "s|/sessions/.*||"
