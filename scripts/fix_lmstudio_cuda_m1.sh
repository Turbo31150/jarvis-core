#!/bin/bash
# Fix LM Studio 0.4.11 — Forcer CUDA sur RTX 3080 (remplace Vulkan)
# À exécuter sur M1 directement

set -e
echo "=== Fix LM Studio CUDA RTX 3080 ==="

# 1. Vérifier nvidia-smi
echo "[1] GPU détecté :"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

# 2. Trouver libcuda
echo "[2] libcuda :"
CUDA_LIB=$(ldconfig -p 2>/dev/null | grep libcuda.so | awk '{print $NF}' | head -1)
if [ -z "$CUDA_LIB" ]; then
    CUDA_LIB=$(find /usr -name "libcuda.so*" 2>/dev/null | head -1)
fi
echo "  → $CUDA_LIB"
CUDA_DIR=$(dirname "$CUDA_LIB")

# 3. Trouver LM Studio
LMSTUDIO_BIN=""
for p in "$HOME/.local/share/applications" "$HOME/.config/autostart" "/usr/share/applications"; do
    f=$(find "$p" -name "*lmstudio*" -o -name "*LMStudio*" 2>/dev/null | head -1)
    if [ -n "$f" ]; then
        LMSTUDIO_BIN=$(grep -oP 'Exec=\K[^ ]+' "$f" 2>/dev/null | head -1)
        break
    fi
done
# Fallback AppImage
if [ -z "$LMSTUDIO_BIN" ]; then
    LMSTUDIO_BIN=$(find "$HOME" -name "*.AppImage" -path "*lmstudio*" 2>/dev/null | head -1)
    [ -z "$LMSTUDIO_BIN" ] && LMSTUDIO_BIN=$(find "$HOME" -name "lmstudio" -type f 2>/dev/null | head -1)
fi
echo "[3] LM Studio binary : $LMSTUDIO_BIN"

# 4. Patcher ~/.bashrc (LD_LIBRARY_PATH + CUDA vars)
echo "[4] Patch ~/.bashrc ..."
grep -q "# JARVIS CUDA FIX" ~/.bashrc || cat >> ~/.bashrc << 'BASHFIX'

# JARVIS CUDA FIX — LM Studio RTX 3080
export CUDA_VISIBLE_DEVICES=0
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH}
export CUDA_HOME=/usr/local/cuda
BASHFIX
echo "  → ~/.bashrc patché"

# 5. Créer wrapper script qui force CUDA
WRAPPER="$HOME/.local/bin/lmstudio-cuda"
mkdir -p "$HOME/.local/bin"
cat > "$WRAPPER" << WRAPPER
#!/bin/bash
# LM Studio avec CUDA forcé
export CUDA_VISIBLE_DEVICES=0
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu:\${LD_LIBRARY_PATH}
export CUDA_HOME=/usr/local/cuda
export LMS_INFERENCE_BACKEND=cuda
exec "${LMSTUDIO_BIN}" "\$@"
WRAPPER
chmod +x "$WRAPPER"
echo "  → Wrapper créé : $WRAPPER"

# 6. Patcher le .desktop autostart pour utiliser CUDA
DESKTOP_SRC=$(find ~/.local/share/applications /usr/share/applications -name "*lmstudio*" -o -name "*LMStudio*" 2>/dev/null | head -1)
DESKTOP_AUTOSTART="$HOME/.config/autostart/lmstudio-cuda.desktop"
mkdir -p "$HOME/.config/autostart"
cat > "$DESKTOP_AUTOSTART" << DESKTOP
[Desktop Entry]
Name=LM Studio (CUDA)
Type=Application
Exec=env CUDA_VISIBLE_DEVICES=0 LD_LIBRARY_PATH=/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu LMS_INFERENCE_BACKEND=cuda ${LMSTUDIO_BIN}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
DESKTOP
echo "  → Desktop autostart créé : $DESKTOP_AUTOSTART"

# 7. Config LM Studio (force GPU layers dans settings)
LMS_CONFIG="$HOME/.lmstudio/config.json"
if [ -f "$LMS_CONFIG" ]; then
    echo "[7] Config LM Studio existante :"
    cat "$LMS_CONFIG" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d['inferenceBackend'] = 'cuda'
d['gpuOffload'] = {'enabled': True, 'layers': -1, 'device': 'cuda:0'}
print(json.dumps(d, indent=2))
" > /tmp/lms_config_patched.json
    cp "$LMS_CONFIG" "${LMS_CONFIG}.bak"
    mv /tmp/lms_config_patched.json "$LMS_CONFIG"
    echo "  → inferenceBackend=cuda, gpuOffload activé"
else
    echo "[7] Pas de config LM Studio trouvée (sera créée au 1er lancement)"
fi

echo ""
echo "=== DONE ==="
echo "Relance LM Studio avec : $WRAPPER"
echo "OU depuis le terminal : source ~/.bashrc && $LMSTUDIO_BIN"
