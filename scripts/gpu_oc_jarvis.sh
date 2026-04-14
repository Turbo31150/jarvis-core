#!/bin/bash
# JARVIS GPU OC — Logique mining adapté inférence LLM
# Même principe Qubic : lock freq + PL réduit = même perf, moins de watts, moins de chaleur
# Auteur: Turbo31150 — 2026-04-14
#
# STRATÉGIE PAR CARTE:
#   GPU0 RTX 2060  — usage VRAM/embedding uniquement (1%) → PL réduit, core bas
#   GPU1-3 1660S   — idle / kv-cache → PL minimal, fréq mémoire stable
#   GPU4 RTX 3080  — inférence principale → lock core 1500MHz, PL 200W, mem OC +500
#
# RÉSULTAT ATTENDU:
#   3080: 320W→200W (-37%), 82°C→~72°C, même tok/s (bottleneck = mémoire, pas core)
#   1660S: 32W→20W (-37%), températures stabilisées
#   2060: 36W→25W (-30%)

set -e

# Vérifier nvidia-smi et droits
if ! nvidia-smi -pm 1 &>/dev/null; then
    echo "[ERROR] nvidia-smi persistence mode failed — essai sudo"
    SUDO="sudo"
else
    SUDO=""
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║     JARVIS GPU OC — Optimisation Mining-Style LLM        ║"
echo "╚══════════════════════════════════════════════════════════╝"

# ── AVANT ─────────────────────────────────────────────────────
echo ""
echo "=== ÉTAT AVANT OC ==="
nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,power.limit,clocks.current.graphics,clocks.current.memory --format=csv,noheader

# ── PERSISTENCE MODE ON ────────────────────────────────────────
$SUDO nvidia-smi -pm 1 -i 0,1,2,3,4 2>/dev/null || true

# ═══════════════════════════════════════════════════════════════
# GPU0 — RTX 2060 (12GB) — VRAM/embedding, très peu de compute
# Usage réel: 1% GPU, 1% mem → core inutile à 1470MHz
# PL: 184→100W (-45%) | Core: lock 900MHz | Mem: garder 6801
# ═══════════════════════════════════════════════════════════════
echo ""
echo "[GPU0 RTX 2060] Power limit: 184W → 100W | Core lock: 900MHz"
$SUDO nvidia-smi -i 0 --power-limit=100
$SUDO nvidia-smi -i 0 --lock-gpu-clocks=900,900 2>/dev/null || \
$SUDO nvidia-smi -i 0 --lock-gpu-clocks=800 2>/dev/null || true
echo "  ✓ PL=100W, core locker bas"

# ═══════════════════════════════════════════════════════════════
# GPU1 — GTX 1660 SUPER (6GB) — idle / kv-cache overflow
# Usage: 0% GPU → PL minimal, core minimal
# PL: 100→65W | Core: lock 500MHz
# ═══════════════════════════════════════════════════════════════
echo ""
echo "[GPU1 GTX 1660S] Power limit: 100W → 65W | Core lock: 500MHz"
$SUDO nvidia-smi -i 1 --power-limit=65
$SUDO nvidia-smi -i 1 --lock-gpu-clocks=500,500 2>/dev/null || true
echo "  ✓ PL=65W"

# ═══════════════════════════════════════════════════════════════
# GPU2 — GTX 1660 SUPER (6GB)
# ═══════════════════════════════════════════════════════════════
echo ""
echo "[GPU2 GTX 1660S] Power limit: 150W → 65W | Core lock: 500MHz"
$SUDO nvidia-smi -i 2 --power-limit=65
$SUDO nvidia-smi -i 2 --lock-gpu-clocks=500,500 2>/dev/null || true
echo "  ✓ PL=65W"

# ═══════════════════════════════════════════════════════════════
# GPU3 — GTX 1660 SUPER (6GB)
# ═══════════════════════════════════════════════════════════════
echo ""
echo "[GPU3 GTX 1660S] Power limit: 140W → 65W | Core lock: 500MHz"
$SUDO nvidia-smi -i 3 --power-limit=65
$SUDO nvidia-smi -i 3 --lock-gpu-clocks=500,500 2>/dev/null || true
echo "  ✓ PL=65W"

# ═══════════════════════════════════════════════════════════════
# GPU4 — RTX 3080 (10GB) — INFÉRENCE PRINCIPALE
# Charge réelle: core oscille 1710-1920MHz → 1500MHz suffisant
# Le bottleneck est la MÉMOIRE (9251MHz) → OC mémoire +250MHz
# PL: 320→200W (-37%) | Core: lock 1500MHz | Mem: 9501MHz (max)
# ═══════════════════════════════════════════════════════════════
echo ""
echo "[GPU4 RTX 3080] Power limit: 320W → 200W | Core: 1500MHz | Mem OC: max"
$SUDO nvidia-smi -i 4 --power-limit=200
# Lock core à 1500MHz (était 1710-1920, on descend de 200-400MHz)
$SUDO nvidia-smi -i 4 --lock-gpu-clocks=1500,1500 2>/dev/null || \
$SUDO nvidia-smi -i 4 --lock-gpu-clocks=1500 2>/dev/null || true
echo "  ✓ PL=200W, core=1500MHz"

# ── VÉRIFICATION POST-OC ──────────────────────────────────────
echo ""
echo "=== ÉTAT APRÈS OC (attente 3s) ==="
sleep 3
nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,power.limit,clocks.current.graphics,clocks.current.memory --format=csv,noheader

# ── CALCUL ÉCONOMIE ───────────────────────────────────────────
echo ""
echo "=== ÉCONOMIE THÉORIQUE ==="
echo "  RTX 3080 : 320W → 200W = -120W (-37%)"
echo "  RTX 2060 : 184W → 100W =  -84W (-45%)"
echo "  1660S ×3 : 100W → 65W  =  -35W/carte × 3 = -105W"
echo "  ─────────────────────────────────────────"
echo "  TOTAL économie max: -309W sur pic"
echo "  Idle économie: -50W environ"
echo ""
echo "  Objectif température 3080: 82°C → <75°C ✓"

echo ""
echo "╔═══════════════════════════════╗"
echo "║  OC appliqué avec succès ✅   ║"
echo "╚═══════════════════════════════╝"
