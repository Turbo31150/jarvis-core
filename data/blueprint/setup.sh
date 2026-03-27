#!/bin/bash
# JARVIS-COMET One-Shot Setup
# Run this on a fresh machine to reproduce the entire system
set -e

echo "🛰️ JARVIS-COMET Setup"

BASE=~/IA/Core/jarvis
mkdir -p $BASE/{config,scripts,data/{workflow-runs,screenshots,blueprint},logs}

# Install Python deps
pip3 install websockets requests beautifulsoup4 2>/dev/null

# Restore DB
[ -f $BASE/data/blueprint/jarvis-master.sql ] && \
    sqlite3 $BASE/data/jarvis-master.db < $BASE/data/blueprint/jarvis-master.sql

# Copy BrowserOS skills
for skill in codeur-scanner linkedin-engage prospect-clients ai-consensus distributed-workflow; do
    mkdir -p ~/.browseros/skills/$skill
done

# Install crons
(crontab -l 2>/dev/null; cat << 'CRON'
*/15 * * * * $BASE/scripts/tmp-monitor.sh
*/30 * * * * cd $BASE && python3 scripts/codeur-veille.py --once >> logs/codeur-veille-cron.log 2>&1
0 */2 * * * cd $BASE && python3 scripts/db_sync.py >> logs/db-sync.log 2>&1
0 9 * * * $BASE/scripts/daily-workflow.sh
@reboot sleep 20 && $BASE/scripts/browseros-ensure-tabs.sh 9108 >> $BASE/logs/browseros-tabs.log 2>&1
CRON
) | sort -u | crontab -

# Systemd service
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/browseros-jarvis.service << SVC
[Unit]
Description=BrowserOS JARVIS
After=network.target
[Service]
Type=simple
ExecStart=$BASE/scripts/browseros-persistent.sh 9108 8080
Restart=on-failure
Environment=DISPLAY=:0
[Install]
WantedBy=default.target
SVC
systemctl --user daemon-reload
systemctl --user enable browseros-jarvis 2>/dev/null

echo "✅ Setup complete"
echo "Next: copy scripts/ and config/ from backup, then run:"
echo "  python3 scripts/db_sync.py"
echo "  browseros-cli health"
