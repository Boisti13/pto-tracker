#!/usr/bin/env bash
# Interactive installer for PTO Tracker.
# Run this from inside a cloned copy of the repo, as root, on a Debian/Ubuntu
# host (a fresh LXC container is the easiest target):
#
#   git clone https://github.com/<you>/pto-tracker.git
#   cd pto-tracker
#   sudo ./install.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run this script as root (e.g. with sudo)." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer expects a Debian/Ubuntu host (apt-get not found)." >&2
  exit 1
fi

echo "== PTO Tracker installer =="
echo

read -rp "Install directory [/opt/pto-tracker]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/opt/pto-tracker}

read -rp "Port to listen on [5000]: " PORT
PORT=${PORT:-5000}

read -rp "System user to run the service as [pto]: " SERVICE_USER
SERVICE_USER=${SERVICE_USER:-pto}

UPDATE_MODE=0
if [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/app.py" ]]; then
  echo
  echo "An existing install was found at $INSTALL_DIR."
  read -rp "Update it in place and keep existing data? [Y/n]: " CONFIRM_UPDATE
  if [[ "${CONFIRM_UPDATE:-Y}" =~ ^[Nn] ]]; then
    echo "Aborting. Remove or choose a different install directory and re-run." >&2
    exit 1
  fi
  UPDATE_MODE=1
fi

echo
echo "Installing to:   $INSTALL_DIR"
echo "Listening on:     0.0.0.0:$PORT"
echo "Running as user:  $SERVICE_USER"
if [[ "$UPDATE_MODE" -eq 1 ]]; then
  echo "Mode:             update (existing data/pto.db is preserved)"
else
  echo "Mode:             fresh install"
fi
read -rp "Proceed? [Y/n]: " CONFIRM
if [[ "${CONFIRM:-Y}" =~ ^[Nn] ]]; then
  echo "Aborted."
  exit 1
fi

echo
echo "--> Installing system packages (python3, venv, pip)..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip >/dev/null

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "--> Creating service user '$SERVICE_USER'..."
  useradd -r -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$SERVICE_USER"
fi

if systemctl is-active --quiet pto-tracker 2>/dev/null; then
  echo "--> Stopping running service for update..."
  systemctl stop pto-tracker
fi

echo "--> Copying application files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
for item in app.py holidays.py requirements.txt templates static; do
  rm -rf "${INSTALL_DIR:?}/${item}"
  cp -r "$REPO_DIR/$item" "$INSTALL_DIR/"
done

echo "--> Setting up Python virtual environment..."
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

mkdir -p "$INSTALL_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "--> Writing systemd service..."
cat > /etc/systemd/system/pto-tracker.service <<EOF
[Unit]
Description=PTO Tracker
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PTO_DB_PATH=$INSTALL_DIR/data/pto.db
ExecStart=$INSTALL_DIR/venv/bin/gunicorn --workers 2 --bind 0.0.0.0:$PORT app:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now pto-tracker >/dev/null

echo
echo "--> Waiting for the service to come up..."
sleep 2
if systemctl is-active --quiet pto-tracker; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo
  echo "Done. PTO Tracker is running."
  echo
  echo "  http://${IP:-<this-host>}:$PORT/"
  echo
  if [[ "$UPDATE_MODE" -eq 0 ]]; then
    echo "Open that URL to complete first-time setup (username, password, allowance)."
  fi
  echo "Check status any time with: systemctl status pto-tracker"
else
  echo "The service did not start. Check logs with:" >&2
  echo "  journalctl -u pto-tracker -n 50 --no-pager" >&2
  exit 1
fi
