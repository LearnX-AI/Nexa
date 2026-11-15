"""School server package installation and synchronisation (NEXA-M2-009).

Controlled, pull-based and resumable: the school server asks core NEXA whether
a newer package exists for its school, downloads a delta (or a full package),
verifies it, builds the new version beside the one being served, and switches
with one atomic write. The served version never changes partially; the
previous version is kept for rollback. The same install path accepts a bundle
file, so a school with no connection can be updated from a USB stick.

Checks before a version is served, any failure leaves the current one in place:
1. signature: HMAC-SHA256 of manifest.json with this school's sync key;
2. school: the package is for this school (NEXA_EDGE_SCHOOL_ID);
3. order: the version is newer than the one installed (never downgrade, except
   an explicit rollback), and a delta applies only to its base version;
4. files: every file matches the sha256 in the manifest;
5. content: after applying a delta, every source's hash equals the manifest's
   target state, and no source is present that the manifest does not list;
6. model: the local embedding model reproduces the core's probe vectors
   (cosine >= 0.999), so local query vectors live in the same space as the
   package's document vectors. If it is the same model computed differently
   (another Ollama version), the school server re-embeds the documents with its
   own model at install, after verifying the content against the core's
   content hashes; a different model altogether is refused.

Conflict and update rules after reconnection:
- core NEXA is the authority for content: a package replaces the school's
  state; nothing on the school server edits content;
- withdrawals are applied with the same package that announces them, so
  content withdrawn while a school was offline disappears at the first sync;
- a school offline for several releases receives a full package when no delta
  from its version exists (deltas are only built from the previous version);
- new school content written in the school's Moodle while offline stays in the
  Moodle plugin's outbox (NEXA-M2-007) and is sent on reconnection; event ids
  make the replay idempotent and the core's revision check rejects stale edits;
  it reaches the school server in the next package, after core approval;
- an interrupted download is resumed or restarted and never installed partly;
- caches of the old version (answers, outlines, spelling) are keyed by version
  and so are never served after a switch.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .edgestore import current_version, package_dir
from .package import SCHEMA, db_sources, file_sha256, fts_body, verify

ProbeFn = Callable[[list[str]], Awaitable[list[list[float]]]]
# Embeds document texts with the school server's model (document prefix applied).
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


class SyncError(Exception):
    pass


@dataclass
class Installed:
    version: int
    kind: str
    from_version: Optional[int]
    sources: int
    chunks: int


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _log(home: str, event: dict[str, Any]) -> None:
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "sync-log.jsonl"), "a") as fh:
        fh.write(json.dumps({"at": _now(), **event}) + "\n")


def history(home: str, n: int = 20) -> list[dict[str, Any]]:
    try:
        with open(os.path.join(home, "sync-log.jsonl")) as fh:
            lines = fh.readlines()[-n:]
        return [json.loads(x) for x in lines]
    except OSError:
        return []


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def unpack(bundle: str, into: str) -> str:
    with tarfile.open(bundle, "r:*") as tar:
        for m in tar.getmembers():
            if not m.isfile() or "/" in m.name or m.name.startswith("."):
                raise SyncError(f"unexpected entry in bundle: {m.name}")
        tar.extractall(into)  # names checked above: flat, regular files only
    return into


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


REEMBED_MIN_COS = 0.8   # below this the local model is not the same model at all


async def install(home: str, src_dir: str, key: bytes, school_id: str, probe: Optional[ProbeFn] = None,
                  allow_downgrade: bool = False, reembed: Optional[EmbedFn] = None) -> Installed:
    """Verify and install the package in src_dir (a full package or a delta);
    on success it is the served version.

    When the local model reproduces the core's probe vectors, the package's
    vectors are served as they are. When it is the same model but a runtime
    computes it differently (another Ollama version: probe cosine below 0.999
    but above REEMBED_MIN_COS), and reembed is given, the school server
    re-embeds the package's documents with its own model, so questions and
    documents share one vector space; content is still verified against the
    core's content hashes. Below REEMBED_MIN_COS the package is refused."""
    try:
        raw = open(os.path.join(src_dir, "manifest.json"), "rb").read()
        sig = open(os.path.join(src_dir, "manifest.sig")).read()
    except OSError as exc:
        raise SyncError(f"not a package: {exc}") from exc
    if not verify(raw, sig, key):
        _log(home, {"event": "rejected", "reason": "bad signature"})
        raise SyncError("signature does not match: package not from core NEXA or altered on the way")
    m = json.loads(raw)
    if m.get("school_id") != school_id:
        raise SyncError(f"package is for school {m.get('school_id')!r}, this server is {school_id!r}")
    if m.get("contains_learner_data") is not False:
        raise SyncError("package does not declare itself free of learner data")
    cur = current_version(home)
    v = int(m["version"])
    if cur is not None and v <= cur and not allow_downgrade:
        raise SyncError(f"version {v} is not newer than the installed version {cur}")
    for name, f in m["files"].items():
        p = os.path.join(src_dir, name)
        if not os.path.exists(p) or file_sha256(p) != f["sha256"]:
            raise SyncError(f"{name} is missing or does not match the manifest")
    local_vectors, worst = False, None
    if probe is not None and m["probe"]["vectors"]:
        got = await probe(m["probe"]["texts"])
        worst = min(_cos(a, b) for a, b in zip(got, m["probe"]["vectors"]))
        if worst < 0.999:
            if reembed is not None and worst >= REEMBED_MIN_COS:
                local_vectors = True
            else:
                _log(home, {"event": "rejected", "version": v, "reason": f"embedding model differs (cos {worst:.4f})"})
                hint = "" if reembed is None or worst >= REEMBED_MIN_COS else " (too different to re-embed: not the same model)"
                raise SyncError(f"the local embedding model does not match the package's (probe cosine {worst:.4f})"
                                f"{hint}; install {m['index']['version']} on this server first")

    dest = package_dir(home, v)
    work = dest + ".building"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    target = os.path.join(work, "knowledge.sqlite")
    if m["kind"] == "full":
        shutil.copyfile(os.path.join(src_dir, "knowledge.sqlite"), target)
    else:
        base = m["base_version"]
        if cur != base:
            shutil.rmtree(work, ignore_errors=True)
            raise SyncError(f"delta applies to version {base}, this server has {cur}: a full package is needed")
        shutil.copyfile(os.path.join(package_dir(home, base), "knowledge.sqlite"), target)
        _apply_delta(target, os.path.join(src_dir, "delta.sqlite"))
    have = db_sources(target)
    want = m["sources"]
    # Content (text, metadata, ids) must be exactly the core's. Vectors are
    # covered by the file hash for what arrived, and may be local for the rest.
    field = "content_sha256" if all("content_sha256" in w for w in want.values()) else "sha256"
    bad = sorted(v_ for v_ in set(have) | set(want)
                 if have.get(v_, {}).get(field) != want.get(v_, {}).get(field))
    if bad:
        shutil.rmtree(work, ignore_errors=True)
        _log(home, {"event": "rejected", "version": v, "reason": f"{len(bad)} sources differ from the manifest"})
        raise SyncError(f"after install {len(bad)} source(s) differ from the manifest (first: {bad[0]})")
    reembedded = 0
    if local_vectors:
        try:
            reembedded = await _reembed(target, reembed)
        except Exception as exc:
            shutil.rmtree(work, ignore_errors=True)
            _log(home, {"event": "rejected", "version": v, "reason": f"re-embedding failed: {type(exc).__name__}"})
            raise SyncError(f"re-embedding with the local model failed ({type(exc).__name__}: {exc})") from exc
        with open(os.path.join(work, "local.json"), "w") as fh:
            json.dump({"vectors": "local", "probe_cosine": round(worst, 4), "reembedded_at": _now()}, fh)
    with open(os.path.join(work, "manifest.json"), "wb") as fh:
        fh.write(raw)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(work, dest)
    if cur is not None and cur != v:
        _write_atomic(os.path.join(home, "previous"), str(cur))
    _write_atomic(os.path.join(home, "current"), str(v))   # the switch
    _prune(home, keep={v, cur})
    res = Installed(v, m["kind"], m.get("base_version"), len(want), m["stats"]["chunks"])
    _log(home, {"event": "installed", "version": v, "kind": m["kind"], "from": cur, "sources": len(want),
                "chunks": res.chunks, "withdrawn": len((m.get("delta") or {}).get("removed", [])),
                "vectors": "local" if local_vectors else "core", "reembedded": reembedded,
                **({"probe_cosine": round(worst, 4)} if worst is not None else {})})
    return res


async def _reembed(target: str, reembed: "EmbedFn", batch: int = 32) -> int:
    """Re-embed every chunk not yet embedded by this server's model (all of a
    full package; after a delta, only the new and changed sources)."""
    from .package import vec_bytes
    con = sqlite3.connect(target)
    try:
        todo = con.execute("SELECT c.chunk_id, coalesce(c.embedding_text, c.text) FROM chunks c "
                           "LEFT JOIN local_vectors l ON l.chunk_id = c.chunk_id WHERE l.chunk_id IS NULL "
                           "ORDER BY c.chunk_id").fetchall()
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            vecs = await reembed([t for _, t in part])
            con.executemany("UPDATE chunks SET vector = ? WHERE chunk_id = ?",
                            [(vec_bytes(v), cid) for (cid, _), v in zip(part, vecs)])
            con.executemany("INSERT OR REPLACE INTO local_vectors VALUES (?)", [(cid,) for cid, _ in part])
            con.commit()
        return len(todo)
    finally:
        con.close()


def _apply_delta(target: str, delta: str) -> None:
    con = sqlite3.connect(target)
    try:
        con.executescript(SCHEMA)
        con.execute("ATTACH DATABASE ? AS d", (delta,))
        changed = [r[0] for r in con.execute("SELECT DISTINCT version_id FROM d.chunks")]
        removed = [r[0] for r in con.execute("SELECT version_id FROM d.removed")]
        for vid in set(changed) | set(removed):
            ids = [r[0] for r in con.execute("SELECT chunk_id FROM chunks WHERE version_id = ?", (vid,))]
            con.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(i,) for i in ids])
            con.executemany("DELETE FROM local_vectors WHERE chunk_id = ?", [(i,) for i in ids])
            con.execute("DELETE FROM chunks WHERE version_id = ?", (vid,))
        rows = con.execute("SELECT chunk_id, version_id, text, metadata, vector, embedding_text FROM d.chunks").fetchall()
        con.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO chunks_fts VALUES (?,?)",
                        [(r[0], fts_body(r[2], json.loads(r[3]))) for r in rows])
        con.commit()
        con.execute("DETACH DATABASE d")
    finally:
        con.close()


def _prune(home: str, keep: set) -> None:
    root = os.path.join(home, "packages")
    for name in os.listdir(root):
        if name.endswith(".building"):
            continue
        try:
            n = int(name.lstrip("v"))
        except ValueError:
            continue
        if n not in keep:
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def rollback(home: str) -> int:
    try:
        prev = int(open(os.path.join(home, "previous")).read().strip())
    except (OSError, ValueError) as exc:
        raise SyncError("no previous version to roll back to") from exc
    if not os.path.exists(os.path.join(package_dir(home, prev), "knowledge.sqlite")):
        raise SyncError(f"previous version {prev} is no longer on disk")
    cur = current_version(home)
    _write_atomic(os.path.join(home, "current"), str(prev))
    if cur is not None:
        _write_atomic(os.path.join(home, "previous"), str(cur))
    _log(home, {"event": "rolled_back", "from": cur, "to": prev})
    return prev


async def install_bundle(home: str, bundle: str, key: bytes, school_id: str, probe: Optional[ProbeFn] = None,
                         reembed: Optional[EmbedFn] = None) -> Installed:
    with tempfile.TemporaryDirectory(prefix="nexa-edge-") as tmp:
        unpack(bundle, tmp)
        return await install(home, tmp, key, school_id, probe, reembed=reembed)


async def pull(home: str, core_url: str, token: str, key: bytes, school_id: str,
               probe: Optional[ProbeFn] = None, client=None, reembed: Optional[EmbedFn] = None) -> Optional[Installed]:
    """Ask core NEXA for this school's latest package and install it if newer.
    Returns None when already up to date. Raises SyncError (and leaves the
    served version unchanged) when the core cannot be reached or the package
    fails a check: the school server keeps serving what it has."""
    import httpx
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=300))
    h = {"Authorization": f"Bearer {token}"}
    base = core_url.rstrip("/") + f"/v1/edge/packages/{school_id}"
    try:
        try:
            r = await client.get(base + "/latest", headers=h)
        except httpx.HTTPError as exc:
            _log(home, {"event": "offline", "reason": type(exc).__name__})
            raise SyncError(f"core NEXA not reachable ({type(exc).__name__}); serving the installed package") from exc
        if r.status_code != 200:
            raise SyncError(f"core NEXA answered {r.status_code}: {r.text[:200]}")
        latest = r.json()
        cur = current_version(home)
        if cur is not None and latest["version"] <= cur:
            _log(home, {"event": "up_to_date", "version": cur})
            return None
        params = {"from": cur} if cur is not None else {}
        with tempfile.TemporaryDirectory(prefix="nexa-edge-") as tmp:
            bundle = os.path.join(tmp, "bundle.tar.gz")
            async with client.stream("GET", base + f"/{latest['version']}/bundle", params=params, headers=h) as resp:
                if resp.status_code != 200:
                    raise SyncError(f"download failed: {resp.status_code}")
                with open(bundle, "wb") as fh:
                    async for block in resp.aiter_bytes():
                        fh.write(block)
            ex = os.path.join(tmp, "x")
            os.makedirs(ex)
            unpack(bundle, ex)
            return await install(home, ex, key, school_id, probe, reembed=reembed)
    finally:
        if own:
            await client.aclose()
