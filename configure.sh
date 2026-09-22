#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec uv run --locked python -m autograb configure
