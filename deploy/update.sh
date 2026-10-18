#!/usr/bin/env bash
# Pull the latest code and restart the bridge (run as root on the server).
set -euo pipefail
sudo -u consensusai git -C /opt/consensusai/app pull --ff-only --quiet
install -m 644 /opt/consensusai/app/deploy/consensusai-bridge.service /etc/systemd/system/
systemctl daemon-reload && systemctl restart consensusai-bridge
systemctl --no-pager --lines=3 status consensusai-bridge
