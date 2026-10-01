#!/usr/bin/env bash
set -euo pipefail
# Dedicated public-research service; existing trading services are untouched.
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python="${RESEARCH_PYTHON:-$repo/.venv/bin/python}"
data="${RESEARCH_DATA_DIR:-$repo/data/research_runtime}"
user="${RESEARCH_USER:-${SUDO_USER:-$(id -un)}}"
port="${RESEARCH_PORT:-63332}"
[[ $EUID -eq 0 ]] || { echo 'Run with sudo; set RESEARCH_USER to the deployment account.'; exit 1; }
[[ -x "$python" && "$port" =~ ^[0-9]+$ ]] || { echo 'Check RESEARCH_PYTHON and RESEARCH_PORT'; exit 1; }
id "$user" >/dev/null
mkdir -p -- "$data"
chown "$user" "$data"
install -d -m 755 /etc/coindcx-research
if [[ ! -e /etc/coindcx-research/alerts.env ]]; then
  install -m 600 /dev/null /etc/coindcx-research/alerts.env
fi
cat > /etc/systemd/system/coindcx-research.service <<EOF
[Unit]
Description=CoinDCX read-only research collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$user
WorkingDirectory=$repo
EnvironmentFile=-/etc/coindcx-research/alerts.env
ExecStart="$python" -u -m app.research.continuous.runtime --data-dir "$data" --port $port
Restart=always
RestartSec=15
TimeoutStopSec=60
KillMode=mixed
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now coindcx-research.service
systemctl --no-pager status coindcx-research.service
