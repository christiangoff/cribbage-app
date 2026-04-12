#!/bin/bash
# Run on the kiosk Raspberry Pi to set up the touchscreen app.
set -e

APP_DIR="/home/pi/cribbage-app/kiosk-app"

echo "==> Installing system dependencies..."
sudo apt update
sudo apt install -y python3-venv python3-pip \
    python3-pyqt5 python3-pyqt5.qtwebengine \
    libqt5webengine5 libqt5webenginecore5 libqt5webenginewidgets5

echo "==> Creating virtualenv with system site-packages..."
cd "$APP_DIR"
python3 -m venv --system-site-packages .venv

echo "==> Setting up config..."
if [ ! -f config.json ]; then
    cp config.json.example config.json
    echo ""
    echo "  *** Edit config.json and set the server URL before running! ***"
    echo "  Example: nano $APP_DIR/config.json"
    echo ""
fi

echo "==> Installing systemd service..."
sudo cp cribbage-kiosk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cribbage-kiosk

echo ""
echo "==> Done!"
echo "    1. Edit config.json with your server's IP address"
echo "    2. Run: sudo systemctl start cribbage-kiosk"
echo "    (or reboot — it will start automatically)"
