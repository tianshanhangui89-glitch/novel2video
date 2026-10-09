#!/usr/bin/env bash
cd "$(dirname "$0")"
if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
else
  PY=python3
fi
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-43123}"
