#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$#" -eq 0 ]; then set -- status; fi
if [ "$(uname -s)" = "Darwin" ]; then
  exec uv run --locked --extra local python -m autograb "$@"
fi
exec uv run --locked python -m autograb "$@"
