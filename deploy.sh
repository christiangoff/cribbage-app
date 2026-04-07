#!/bin/bash
# Run this on the Raspberry Pi to set up / update the app.
set -e

APP_DIR="/home/pi/cribbage-app"

echo "==> Pulling latest code..."
cd "$APP_DIR"
git pull

echo "==> Installing dependencies..."
.venv/bin/pip install -r requirements.txt

echo "==> Initialising/migrating database..."
.venv/bin/flask --app app init-db

echo "==> Restarting service..."
sudo systemctl restart cribbage

echo "==> Done. Status:"
sudo systemctl status cribbage --no-pager
