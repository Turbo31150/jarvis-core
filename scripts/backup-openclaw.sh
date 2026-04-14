#!/bin/bash
DATE=$(date +%Y-%m-%d-%H%M)
BACKUP_DIR=~/Backups/openclaw
mkdir -p "$BACKUP_DIR"

tar -czf "$BACKUP_DIR/openclaw-$DATE.tar.gz" \
  ~/.openclaw/ \
  ~/IA/Core/jarvis/scripts/ \
  --exclude='*.pyc' \
  --exclude='__pycache__' \
  --exclude='*.log' \
  2>/dev/null

# Garder les 7 derniers backups
ls -t "$BACKUP_DIR"/openclaw-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm

echo "✅ Backup: $BACKUP_DIR/openclaw-$DATE.tar.gz"
ls -lh "$BACKUP_DIR/openclaw-$DATE.tar.gz" 2>/dev/null
