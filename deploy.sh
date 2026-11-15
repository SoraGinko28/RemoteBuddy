#!/usr/bin/env bash
# One-shot miniconda deployment for RemoteBuddy.
#
#   ./deploy.sh            # create/update the env, run doctor, start the web UI
#   ./deploy.sh --check    # create/update the env and run doctor only
#
# Override defaults with CONDA_ROOT=... ENV_NAME=... PORT=...
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

CONDA_ROOT="${CONDA_ROOT:-$HOME/miniconda3}"
ENV_NAME="${ENV_NAME:-remotebuddy}"
PORT="${PORT:-8765}"
CONDA="$CONDA_ROOT/bin/conda"
ENV_PREFIX="$CONDA_ROOT/envs/$ENV_NAME"
PY="$ENV_PREFIX/bin/python"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "checking prerequisites"
if [ ! -x "$CONDA" ]; then
    echo "error: miniconda not found at $CONDA_ROOT (set CONDA_ROOT to override)" >&2
    exit 1
fi
for tool in tmux vim; do
    command -v "$tool" >/dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done

step "creating/updating conda env '$ENV_NAME' (conda-forge, python 3.12)"
if [ ! -d "$ENV_PREFIX" ]; then
    "$CONDA" create -y -n "$ENV_NAME" --override-channels -c conda-forge python=3.12 pip
fi
"$PY" -m pip install --quiet -r requirements.txt
"$PY" -c 'import sys, libtmux, mcp; print(f"python {sys.version.split()[0]}, libtmux {libtmux.__version__}, mcp OK")'

step "checking model_api configuration"
missing=0
for f in model_api/config.toml model_api/auth.json; do
    if [ ! -f "$f" ]; then
        echo "warning: $f is missing — create it from ${f%.*}.example.${f##*.}"
        missing=1
    fi
done
[ -f model_api/auth.json ] && chmod 600 model_api/auth.json

step "running doctor"
"$PY" demo.py doctor || true

if [ "${1:-}" = "--check" ]; then
    echo
    echo "environment is ready; start the UI with: ./deploy.sh"
    exit 0
fi
if [ "$missing" -eq 1 ]; then
    echo
    echo "note: the web UI still starts; API keys can also be entered from the New Session dialog."
fi

step "starting RemoteBuddy on http://127.0.0.1:$PORT/"
exec "$PY" rb.py web --port "$PORT"
