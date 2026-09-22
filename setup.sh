#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v uv >/dev/null 2>&1 || { echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/'; exit 1; }
uv sync --locked --python 3.12
uv run --locked python -m playwright install chromium
echo 'AutoGrab setup complete. Start with ./start.sh'
