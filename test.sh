#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec uv run --locked --all-extras python -m unittest discover -s tests -v
