#!/bin/bash
# Lance le RPC server sur M1 — expose tous les GPUs CUDA au réseau
# M2 (WIN-TBOT) se connecte ici pour utiliser les GPUs de M1
# Port 50052 (standard llama.cpp RPC)

RPC_SERVER=/tmp/llama_rpc_build/build/bin/rpc-server
LOG=/var/log/jarvis-rpc-server.log

# Vérifier que le binaire existe
if [ ! -f "$RPC_SERVER" ]; then
    echo "ERROR: rpc-server non compilé. Lancer: cmake --build build --target rpc-server"
    exit 1
fi

# Exposer tous les GPU CUDA
export CUDA_VISIBLE_DEVICES=0,1,2,3,4  # RTX2060,1660S,1660S,1660S,RTX3080

echo "[$(date)] Démarrage RPC server M1 sur 0.0.0.0:50052"
echo "[$(date)] GPUs exposés: RTX2060(12G) + RTX3080(10G) + 4×1660S(6G) = 46GB"
exec "$RPC_SERVER" \
    --host 0.0.0.0 \
    --port 50052 \
    2>&1 | tee -a "$LOG"
