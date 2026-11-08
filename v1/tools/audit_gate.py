"""Dependency and security scan gate with time-boxed exceptions (NEXA-M1-009).

    python v1/tools/audit_gate.py [--today YYYY-MM-DD]

Runs pip-audit over every service requirements file and fails when:
- an advisory is found that v1/security/audit-exceptions.json does not list
  for that exact package version (matched on id or any alias),
- an exception has expired,
- an exception names a package version that is no longer pinned (stale).

Exit 0 only when every finding is covered by a live, current exception.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXCEPTIONS = os.path.join(ROOT, "v1", "security", "audit-exceptions.json")


def run_audit(files: list[str]) -> list[dict]:
    cmd = [sys.executable, "-m", "pip_audit", "--progress-spinner", "off", "-f", "json"]
    for f in files:
        cmd += ["-r", f]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # pip-audit exits 1 both when it finds advisories and when it crashes, so
    # only a parseable report counts as a result. Anything else fails the gate.
    try:
        return json.loads(proc.stdout)["dependencies"]
    except (json.JSONDecodeError, KeyError):
        print("\n".join(proc.stderr.strip().splitlines()[-15:]), file=sys.stderr)
        raise SystemExit(f"Dependency audit gate FAILED: pip-audit did not produce a report "
                         f"(exit {proc.returncode})")


def evaluate(deps: list[dict], exceptions: list[dict], today: dt.date) -> list[str]:
    problems: list[str] = []
    pinned = {(d["name"].lower(), d.get("version")) for d in deps}
    live = {}
    for e in exceptions:
        key = (e["package"].lower(), e["version"])
        if dt.date.fromisoformat(e["expires"]) < today:
            problems.append(f"exception for {key[0]} {key[1]} expired on {e['expires']}")
            continue
        if key not in pinned:
            problems.append(f"stale exception: {key[0]} {key[1]} is no longer pinned; remove the entry")
            continue
        live[key] = set(e["ids"])
    for d in deps:
        key = (d["name"].lower(), d.get("version"))
        for v in d.get("vulns", []):
            ids = {v["id"], *v.get("aliases", [])}
            if not ids & live.get(key, set()):
                fix = ", ".join(v.get("fix_versions", [])) or "none"
                problems.append(f"{key[0]} {key[1]}: {v['id']} not covered (fix: {fix})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--today", default=None)
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    files = sorted(glob.glob(os.path.join(ROOT, "v1", "*", "requirements.txt")))
    with open(EXCEPTIONS) as fh:
        exceptions = json.load(fh)["exceptions"]
    deps = run_audit(files)
    problems = evaluate(deps, exceptions, today)
    covered = sum(len(d.get("vulns", [])) for d in deps)
    if problems:
        print("Dependency audit gate FAILED:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"Dependency audit gate OK ({covered} advisories covered by live exceptions, "
          f"{len(exceptions)} exception(s))")
    for e in exceptions:
        days = (dt.date.fromisoformat(e["expires"]) - today).days
        print(f"  {e['package']} {e['version']}: expires {e['expires']} ({days} days)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
