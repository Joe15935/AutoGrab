#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec uv run --locked python -m unittest discover -s tests -v
