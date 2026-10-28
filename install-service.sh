#!/usr/bin/env bash
# Installe le service systemd « nextstep-migration » (démarrage automatique, redémarrage en cas d'erreur)
set -euo pipefail
cd "$(dirname "$0")"
DIR=$PWD; USER_NAME=$(id -un)
UNIT=/etc/systemd/system/nextstep-migration.service
sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=NextStep Migration Suite (PST vers Zimbra)
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/run.sh
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT_EOF
sudo systemctl daemon-reload
sudo systemctl enable --now nextstep-migration
sleep 2
systemctl --no-pager --lines=5 status nextstep-migration || true
echo
echo "Commandes utiles : sudo systemctl restart|stop|status nextstep-migration ; journalctl -u nextstep-migration -f"
