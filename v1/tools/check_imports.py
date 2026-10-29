"""Import-rule check (NEXA-M1-001 section 6).

Fails if a forbidden import appears outside its allowed home:
- model SDKs (openai, ollama) only inside v1/model_gateway/backends/
- no service imports another service's internal modules

Run: python v1/tools/check_imports.py
Exit non-zero on any violation so CI fails the build.
"""
from __future__ import annotations
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
V1 = ROOT / "v1"

# SDK name -> the only directory it may be imported from (relative to repo root)
SDK_HOME = {
    "openai": "v1/model_gateway/backends",
    "ollama": "v1/model_gateway/backends",
    "diffusers": "v1/tool_gateway",   # image tool, when it exists
    "transformers": "v1/tool_gateway",
}

# Service packages that must not import each other's internals. Importing the
# shared pure helpers from v1.identity, v1.observability and v1.shared is fine.
SERVICES = [
    "api_gateway", "model_gateway", "agent_runtime", "knowledge",
    "memory", "tool_gateway", "safety_eval",
]

violations: list[str] = []

for path in V1.rglob("*.py"):
    rel = path.relative_to(ROOT).as_posix()
    if "/tests/" in rel or "/tools/" in rel:
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    for lineno, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(?:from|import)\s+([a-zA-Z0-9_\.]+)", line)
        if not m:
            continue
        mod = m.group(1)
        head = mod.split(".")[0]
        # SDK confinement
        if head in SDK_HOME and not rel.startswith(SDK_HOME[head]):
            violations.append(f"{rel}:{lineno}: import of {head} outside {SDK_HOME[head]}")
        # Cross-service internal imports: v1.<service>.<module> from another service
        cm = re.match(r"v1\.([a-z_]+)\.", mod)
        if cm:
            target = cm.group(1)
            here = rel.split("/")[1] if rel.startswith("v1/") else ""
            if (
                target in SERVICES
                and target != here
                and target not in ("identity", "observability", "shared")
            ):
                violations.append(
                    f"{rel}:{lineno}: cross-service import of v1.{target} from {here}"
                )

if violations:
    print("Import-rule violations:")
    for v in violations:
        print("  " + v)
    sys.exit(1)
print("Import rules OK")
