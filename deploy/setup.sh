#!/usr/bin/env bash
# One-time server setup (Ubuntu 24.04, run as root). Secrets go in /etc/consensusai/*.env separately.
# Update later with: deploy/update.sh
set -euo pipefail
BRANCH="${1:-main}"
export DEBIAN_FRONTEND=noninteractive
cloud-init status --wait >/dev/null 2>&1 || true
apt-get update -qq && apt-get install -y -qq python3-venv git >/dev/null
id consensusai >/dev/null 2>&1 || useradd --system --home-dir /opt/consensusai --shell /usr/sbin/nologin consensusai
mkdir -p /opt/consensusai /etc/consensusai && chmod 700 /etc/consensusai
[ -d /opt/consensusai/app/.git ] || git clone --quiet --branch "$BRANCH" https://github.com/daviskennedy10/consensusai.git /opt/consensusai/app
[ -x /opt/consensusai/venv/bin/python ] || python3 -m venv /opt/consensusai/venv
chown -R consensusai:consensusai /opt/consensusai
# Key-only SSH
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
rm -f /etc/ssh/sshd_config.d/50-cloud-init.conf && systemctl reload ssh
install -m 644 /opt/consensusai/app/deploy/consensusai-bridge.service /etc/systemd/system/
systemctl daemon-reload
echo "setup done: python $(/opt/consensusai/venv/bin/python -V 2>&1), branch $BRANCH"
