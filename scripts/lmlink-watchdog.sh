#!/bin/bash
# LM Link watchdog — reconnecte si déconnecté, recharge les modèles sur WIN-TBOT
LMS=/home/turbo/.lmstudio/bin/lms
LOG=/var/log/jarvis-lmlink-watchdog.log

check_and_fix() {
    STATUS=$($LMS link status 2>&1)
    
    if echo "$STATUS" | grep -q "Status: connected"; then
        # Vérifier que les modèles sont chargés sur WIN-TBOT
        PS=$($LMS ps 2>&1)
        WINTBOT_COUNT=$(echo "$PS" | grep "WIN-TBOT" | wc -l)
        LOCAL_COUNT=$(echo "$PS" | grep "Local" | wc -l)
        
        # Décharger tout ce qui est en local
        if [ "$LOCAL_COUNT" -gt 0 ]; then
            echo "[$(date)] Modèles en local détectés ($LOCAL_COUNT), déchargement..." | tee -a $LOG
            echo "$PS" | grep "Local" | awk '{print $1}' | while read id; do
                $LMS unload "$id" 2>/dev/null
            done
            sleep 2
            $LMS load "qwen/qwen3.5-9b" --gpu max --parallel 4 2>/dev/null &
            $LMS load "deepseek/deepseek-r1-0528-qwen3-8b" --gpu max --parallel 4 2>/dev/null &
            echo "[$(date)] Rechargement sur WIN-TBOT lancé" | tee -a $LOG
        fi
        
        if [ "$WINTBOT_COUNT" -eq 0 ]; then
            echo "[$(date)] Aucun modèle sur WIN-TBOT, chargement..." | tee -a $LOG
            $LMS load "qwen/qwen3.5-9b" --gpu max --parallel 4 2>/dev/null &
            $LMS load "deepseek/deepseek-r1-0528-qwen3-8b" --gpu max --parallel 4 2>/dev/null &
        fi
    else
        echo "[$(date)] LM Link déconnecté, tentative reconnexion..." | tee -a $LOG
        # Tuer et relancer le connector
        pkill -f lmlink-connector 2>/dev/null
        sleep 2
        /home/turbo/.lmstudio/extensions/frameworks/lmlink-connector-linux-x86_64-avx2-0.1.0/lmlink-connector &
        sleep 5
        # Recharger les modèles
        $LMS load "qwen/qwen3.5-9b" --gpu max --parallel 4 2>/dev/null &
        $LMS load "deepseek/deepseek-r1-0528-qwen3-8b" --gpu max --parallel 4 2>/dev/null &
        echo "[$(date)] Reconnexion tentée" | tee -a $LOG
    fi
}

check_and_fix
