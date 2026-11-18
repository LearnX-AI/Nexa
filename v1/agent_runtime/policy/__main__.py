"""python -m v1.agent_runtime.policy validate | schema   (NEXA-M3-007)

validate: every shipped policy names a shipped agent and stays within its
manifest and the tool contracts; every shipped agent has a policy.
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    from . import contract as pc
    if argv[:1] == ["schema"]:
        print(json.dumps(pc.json_schema(), indent=2))
        return 0
    from .. import registry
    from . import pdp
    found = pdp.load(strict=True)
    missing = sorted(set(registry.MANIFESTS) - set(found))
    for a, p in sorted(found.items()):
        print(f"ok  {a:<24} invoke={sorted(p.invoke)} read={p.read.knowledge_classes} delegate={p.delegate} "
              f"accept_delegation={p.accept_delegation} redact={p.sensitive.redact_personal_data}  "
              f"{p.fingerprint()[:19]}")
    if missing:
        print(f"agents without a policy (they can do nothing): {missing}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
