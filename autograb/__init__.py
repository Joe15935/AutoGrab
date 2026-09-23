"""Lightweight opportunity monitoring; checkout assistance is optional."""

import sys
import time

# QUERY must not create bytecode files on a fresh installation either.
sys.dont_write_bytecode = True
STARTED_AT = time.perf_counter()
__version__ = "0.6.0a0"


def run_provider(provider, *, root=None, mode=None, qinglong=False, argv=None):
    """Shared one-shot entry point. QingLong wrappers cannot dispatch Edge."""
    from .script_cli import run_provider as run
    return run(provider, root=root, mode=mode, qinglong=qinglong, argv=argv)
