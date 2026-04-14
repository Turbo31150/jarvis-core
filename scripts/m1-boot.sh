#!/bin/bash
# Démarrage propre M1 LM Studio — 1 modèle, pas d'auth, pas de surcharge
# Appelé par systemd au boot

# Verrou anti-race-condition : une seule instance à la fois
LOCKFILE=/tmp/m1-boot-lms.lock
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "[M1-BOOT] Déjà en cours d'exécution — abandon"
    exit 0
fi

# Attendre que LM Studio GUI soit prêt (si actif)
sleep 15

# Décharger tout ce qui tourne (évite les doublons)
~/.lmstudio/bin/lms unload --all 2>/dev/null || true
sleep 3

# Redémarrer le serveur headless sans auth
~/.lmstudio/bin/lms server stop 2>/dev/null || true
sleep 2
~/.lmstudio/bin/lms server start --port 1234 --bind 0.0.0.0 --cors 2>&1

# Charger UN seul modèle (qwen3.5-9b = 6.5GB, reste de la RAM pour autres tâches)
~/.lmstudio/bin/lms load "qwen/qwen3.5-9b" --context-length 8192 --gpu max 2>&1 &
MODEL_PID=$!

# Attendre que le modèle soit chargé (max 120s)
for i in $(seq 1 24); do
    sleep 5
    if ~/.lmstudio/bin/lms ps 2>/dev/null | grep -q "IDLE\|READY"; then
        echo "[M1-BOOT] Modèle chargé OK"
        # Vérifier API
        MODELS=$(curl -s http://127.0.0.1:1234/v1/models 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null)
        echo "[M1-BOOT] API models: ${MODELS:-0}"
        break
    fi
done

echo "[M1-BOOT] Démarrage terminé"
