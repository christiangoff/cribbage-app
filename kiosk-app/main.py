#!/usr/bin/env python3
"""
Cribbage Kiosk — standalone Pi touchscreen app.
Reads server URL from config.json and opens a fullscreen embedded browser.
"""
import json
import os
import sys

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QCursor
from PyQt5.QtWebEngineWidgets import QWebEngineProfile, QWebEngineSettings, QWebEngineView
from PyQt5.QtWidgets import QApplication

BASE = os.path.dirname(os.path.abspath(__file__))

# ── Load config ───────────────────────────────────────────────────────────
cfg_path = os.path.join(BASE, "config.json")
if not os.path.exists(cfg_path):
    print(f"ERROR: config.json not found at {cfg_path}")
    print("Copy config.json.example to config.json and set your server URL.")
    sys.exit(1)

with open(cfg_path) as f:
    config = json.load(f)

SERVER = config.get("server", "").rstrip("/")
if not SERVER:
    print("ERROR: 'server' key missing or empty in config.json")
    sys.exit(1)

# ── Qt app ────────────────────────────────────────────────────────────────
app = QApplication(sys.argv)
app.setApplicationName("Cribbage Kiosk")

view = QWebEngineView()

# Allow local HTML to make cross-origin requests to the server
settings = view.page().settings()
settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
settings.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, False)

# Touch-friendly: hide mouse cursor (Pi touchscreen doesn't need it)
if config.get("hide_cursor", True):
    app.setOverrideCursor(QCursor(Qt.BlankCursor))

# Fullscreen frameless window
view.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
view.showFullScreen()

# Load local kiosk.html, pass server URL as query param
html_path = os.path.join(BASE, "kiosk.html")
url = QUrl.fromLocalFile(html_path)
url.setQuery(f"server={SERVER}")
view.load(url)

print(f"Cribbage Kiosk started — connecting to {SERVER}")
sys.exit(app.exec_())
