"""NEXA-M2-009 school edge tool.

Core NEXA (DGX), with the knowledge service environment loaded:

    python -m v1.edge.cli build --school pomnsoe [--no-teacher-only] [--subjects Biology,Physics] [--grades 11,12]
    python -m v1.edge.cli school-key --school pomnsoe --out ~/pomnsoe.key     # provision a school server

School server:

    python -m v1.edge.cli install nexa-edge-pomnsoe-v3-full.tar.gz   # from a file (scp, USB)
    python -m v1.edge.cli pull                                    # from core NEXA
    python -m v1.edge.cli rollback
    python -m v1.edge.cli status

Environment: NEXA_EDGE_MASTER_KEY (core, 64 hex), NEXA_EDGE_PACKAGE_DIR (core);
NEXA_EDGE_HOME, NEXA_EDGE_SCHOOL_ID, NEXA_EDGE_SYNC_KEY, NEXA_EDGE_CORE_URL,
NEXA_EDGE_CORE_TOKEN (school server).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _master() -> bytes:
    k = os.environ.get("NEXA_EDGE_MASTER_KEY", "")
    if len(k) != 64:
        sys.exit("NEXA_EDGE_MASTER_KEY must be 64 hex characters (python3 -c 'import secrets;print(secrets.token_hex(32))')")
    return bytes.fromhex(k)


async def _build(a) -> int:
    from ..knowledge import config, embeddings
    from ..knowledge.store import get_store
    from .package import PROBE_TEXTS, Policy, build, collect, school_key
    out = a.out or config.EDGE_PACKAGE_DIR
    if not out:
        sys.exit("set NEXA_EDGE_PACKAGE_DIR or --out")
    store = get_store()
    await store.open()
    try:
        iid = await store.active_index_id()
        rec = await store.get_index(iid) if iid else None
        if rec is None:
            sys.exit("no active index on core NEXA")
        pol = Policy(school_id=a.school, province=a.province, include_teacher_only=not a.no_teacher_only,
                     subjects=[x for x in (a.subjects or "").split(",") if x],
                     grades=[int(x) for x in (a.grades or "").split(",") if x],
                     languages=[x for x in (a.languages or "").split(",") if x])
        items, skipped = await collect(store, iid, pol)
    finally:
        await store.close()
    probes, _ = await embeddings._gateway(list(PROBE_TEXTS), rec.logical, None)
    root = os.path.join(out, a.school)
    prev = None
    versions = sorted(int(n[1:]) for n in os.listdir(root) if n.startswith("v") and n[1:].isdigit()) \
        if os.path.isdir(root) else []
    if versions:
        with open(os.path.join(root, f"v{versions[-1]}", "full", "manifest.json")) as fh:
            prev = json.load(fh)
    version = (versions[-1] + 1) if versions else 1
    index = {"index_id": rec.index_id, "fingerprint": rec.fingerprint, "logical": rec.logical,
             "version": rec.version, "runtime": rec.runtime, "dim": rec.dim, "prefix_scheme": rec.prefix_scheme}
    res = build(items, policy=pol, index=index, version=version, out_root=out, key=school_key(_master(), a.school),
                probes=probes, skipped=skipped, previous=prev)
    for r in res:
        m = r.manifest
        size = sum(f["bytes"] for f in m["files"].values())
        extra = ""
        if r.kind == "delta":
            extra = f", {len(m['delta']['changed'])} changed/new sources, {len(m['delta']['removed'])} withdrawn"
        print(f"{r.kind:5} v{m['version']}: {m['stats']['sources']} sources, {m['stats']['chunks']} chunks, "
              f"{size / 1e6:.1f} MB{extra}\n      {r.bundle}")
    print("excluded:", json.dumps(skipped))
    return 0


def _school_key(a) -> int:
    from .package import school_key
    k = school_key(_master(), a.school).hex()
    fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"NEXA_EDGE_SYNC_KEY={k}\n")
    print(f"wrote the sync key for {a.school} to {a.out} (mode 600); copy it to the school server's env file")
    return 0


async def _edge(a) -> int:
    from ..knowledge import config
    from . import sync
    from .web import embed_probe, sync_key
    home, school = config.EDGE_HOME, config.EDGE_SCHOOL_ID
    if not school:
        sys.exit("NEXA_EDGE_SCHOOL_ID is not set")
    probe = None if a.no_probe else embed_probe
    try:
        if a.cmd == "install":
            r = await sync.install_bundle(home, a.bundle, sync_key(), school, probe)
            print(f"installed v{r.version} ({r.kind}{f' from v{r.from_version}' if r.from_version else ''}): "
                  f"{r.sources} sources, {r.chunks} chunks")
        elif a.cmd == "pull":
            r = await sync.pull(home, config.EDGE_CORE_URL, config.EDGE_CORE_TOKEN, sync_key(), school, probe)
            print("up to date" if r is None else f"installed v{r.version} ({r.kind}): {r.sources} sources")
        elif a.cmd == "rollback":
            print(f"now serving v{sync.rollback(home)}")
    except sync.SyncError as exc:
        print(f"not installed: {exc}", file=sys.stderr)
        return 1
    return 0


def _status(a) -> int:
    from ..knowledge import config
    from .edgestore import current_version, package_dir
    from .sync import history
    v = current_version(config.EDGE_HOME)
    print(f"school {config.EDGE_SCHOOL_ID or '?'}: serving v{v}" if v else "no package installed")
    if v:
        with open(os.path.join(package_dir(config.EDGE_HOME, v), "manifest.json")) as fh:
            m = json.load(fh)
        print(f"  built {m['created_at']}, {m['stats']['sources']} sources, {m['stats']['chunks']} chunks, "
              f"model {m['index']['fingerprint']}")
    for h in history(config.EDGE_HOME, 8):
        print("  ", json.dumps(h))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--school", required=True)
    b.add_argument("--province")
    b.add_argument("--no-teacher-only", action="store_true")
    b.add_argument("--subjects")
    b.add_argument("--grades")
    b.add_argument("--languages")
    b.add_argument("--out")
    k = sub.add_parser("school-key")
    k.add_argument("--school", required=True)
    k.add_argument("--out", required=True)
    i = sub.add_parser("install")
    i.add_argument("bundle")
    for name in ("install", "pull"):
        (i if name == "install" else sub.add_parser("pull")).add_argument("--no-probe", action="store_true",
                                                                         help="skip the embedding model check")
    sub.add_parser("rollback").set_defaults(no_probe=True)
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "build":
        return asyncio.run(_build(a))
    if a.cmd == "school-key":
        return _school_key(a)
    if a.cmd == "status":
        return _status(a)
    return asyncio.run(_edge(a))


if __name__ == "__main__":
    raise SystemExit(main())
