#!/bin/bash
# jpte-master — Point d'entrée unique cluster stratégique JARVIS
# Usage: jpte-master [once|watch|status|dispatch <pipeline>|ask "<demande>"|jpte "<demande>"]

JPTE_DIR="/home/turbo/IA/Core/jarvis/scripts/jpte"
PYTHON="python3"

case "${1:-once}" in
  once)
    $PYTHON "$JPTE_DIR/adaptive_trigger.py" --once
    ;;
  watch)
    echo "[JPTE-MASTER] Watch mode — Ctrl+C pour arrêter"
    $PYTHON "$JPTE_DIR/adaptive_trigger.py" --watch
    ;;
  status)
    $PYTHON "$JPTE_DIR/adaptive_trigger.py" --status
    echo ""
    echo "[Agents OpenClaw disponibles]"
    openclaw agents list 2>/dev/null | grep "^-" | sed 's/- /  /' | sed 's/ (default)//'
    ;;
  dispatch)
    PIPELINE="${2:-cluster}"
    echo "[JPTE-MASTER] Dispatch → openclaw-master (pipeline=$PIPELINE)"
    openclaw agent --agent openclaw-master --message "[MANUAL] pipeline=$PIPELINE"
    ;;
  ask)
    DEMANDE="${2:-help}"
    echo "[JPTE-MASTER] Demande → openclaw-master"
    openclaw agent --agent openclaw-master --message "$DEMANDE"
    ;;
  jpte)
    DEMANDE="${2:-status}"
    $PYTHON "$JPTE_DIR/jpte.py" "$DEMANDE"
    ;;
  *)
    echo "Usage: jpte-master [once|watch|status|dispatch <pipeline>|ask <demande>|jpte <demande>]"
    echo ""
    echo "  once              Cycle adaptatif unique (défaut)"
    echo "  watch             Boucle continue 5 min"
    echo "  status            Flux + agents disponibles"
    echo "  dispatch PIPELINE Route vers openclaw-master"
    echo "  ask \"demande\"     Demande libre via agent maître"
    echo "  jpte \"demande\"    JPTE pipeline complet"
    ;;
esac
