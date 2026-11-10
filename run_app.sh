#!/usr/bin/env bash
# Run the PulseBrief demo on macOS/Linux.
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python"
fi

echo "Starting PulseBrief with $PY ..."
exec "$PY" -m streamlit run app.py "$@"
