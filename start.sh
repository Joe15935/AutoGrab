#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$#" -eq 0 ]; then set -- monitor; fi
exec uv run --locked python -m autograb "$@"
