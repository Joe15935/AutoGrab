"""QingLong: Bandwagon public monitoring only."""
import sys
from autograb import run_provider

if __name__ == "__main__":
    raise SystemExit(run_provider("bandwagon", qinglong=True, argv=sys.argv[1:]))
