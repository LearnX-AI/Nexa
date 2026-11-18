"""Apply the state retention policy now (NEXA-M3-006): delete expired sessions,
and the working state of runs that ended more than the retention period ago.

    python -m v1.tools.state_retention --dry-run
    python -m v1.tools.state_retention --days 30
"""
from __future__ import annotations

import argparse
import json
import sys

from v1.agent_runtime import registry
from v1.agent_runtime.state import purge


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=None, help="retention in days (default NEXA_STATE_RETENTION_DAYS)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if registry.REGISTRY is None:
        registry.reload()
    print(json.dumps(purge.purge(days=a.days, dry_run=a.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
