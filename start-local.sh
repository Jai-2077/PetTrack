#!/usr/bin/env sh
# Laptop launcher (Linux/macOS). Edit the exports or use a .env you source yourself.
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
# export PETTRACK_PORT=/dev/ttyUSB0
# export PETTRACK_CLOUD_URL=https://your-app.onrender.com
# export PETTRACK_SYNC_KEY=long-random-string
python run.py
