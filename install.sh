#!/usr/bin/env bash
# Installs uptime-tracker to /opt/uptime-tracker and registers systemd services.
# Run as root: sudo bash install.sh

set -euo pipefail

INSTALL_DIR="/opt/uptime-tracker"
DATA_DIR="$INSTALL_DIR/data"
SERVICE_USER="uptime-tracker"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: Run as root (sudo bash install.sh)" >&2
  exit 1
fi

echo "==> Creating system user '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Installing files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR" "$DATA_DIR"

cp "$SCRIPT_DIR/pinger.py" "$INSTALL_DIR/pinger.py"
cp "$SCRIPT_DIR/server.py"  "$INSTALL_DIR/server.py"

chmod +x "$INSTALL_DIR/pinger.py"
chmod +x "$INSTALL_DIR/server.py"

# Create config.json only if it doesn't already exist (preserve user settings on reinstall)
if [[ ! -f "$INSTALL_DIR/config.json" ]]; then
  echo '{"host":"8.8.8.8"}' > "$INSTALL_DIR/config.json"
  echo "    Created $INSTALL_DIR/config.json"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Installing systemd units..."
cp "$SCRIPT_DIR/uptime-pinger.service" /etc/systemd/system/uptime-pinger.service
cp "$SCRIPT_DIR/uptime-web.service"    /etc/systemd/system/uptime-web.service

echo ""
echo "==> Current configuration (edit /etc/systemd/system/uptime-pinger.service"
echo "    and /etc/systemd/system/uptime-web.service to change):"
grep '^Environment=' /etc/systemd/system/uptime-pinger.service
grep '^Environment=' /etc/systemd/system/uptime-web.service
echo ""

systemctl daemon-reload

echo "==> Enabling and starting services..."
systemctl enable --now uptime-pinger.service
systemctl enable --now uptime-web.service

echo ""
echo "==> Done."
echo ""
systemctl --no-pager status uptime-pinger.service uptime-web.service

# Detect the machine's LAN IP for convenience
LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7; exit}')
PORT=$(grep 'UPTIME_PORT' /etc/systemd/system/uptime-web.service | cut -d= -f2)
PORT=${PORT:-9090}

echo ""
echo "Dashboard: http://${LAN_IP:-<your-ip>}:${PORT}"
echo ""
echo "Useful commands:"
echo "  sudo journalctl -u uptime-pinger -f   # follow pinger log"
echo "  sudo journalctl -u uptime-web -f       # follow web log"
echo "  sudo systemctl restart uptime-pinger   # restart pinger"
echo "  sudo systemctl restart uptime-web      # restart web"
echo ""
echo "To change the monitored host:"
echo "  sudo systemctl edit uptime-pinger"
echo "  sudo systemctl edit uptime-web"
echo "  (add [Service] + Environment=UPTIME_HOST=<new-host> in the override file)"
