"""School edge knowledge packages (NEXA-M2-009).

Central improvement, local delivery: core NEXA (the DGX) ingests, governs,
embeds and evaluates; each school NEXA server receives a signed, versioned
package holding exactly the approved knowledge that school may use, and
searches it locally with no dependency on the WAN.

A package is a directory (or a gzipped .tar.gz of one, for transfer):

    manifest.json   what the package is (school, version, embedding model,
                    policy, the sources it holds with a hash of each, file
                    hashes, probe vectors for the model check)
    manifest.sig    HMAC-SHA256 of manifest.json with the school's sync key
    knowledge.sqlite   full package: every chunk (text, metadata, float16
                    vector) plus an FTS5 index for lexical search
  or
    delta.sqlite    changes since base_version: the chunks of sources that
                    were added or changed, and the sources that were removed

The manifest always lists the complete target state (every source and its
hash), so a school server applying a delta can prove it reached exactly the
state the core built, and fall back to a full package if not.

What goes in (select()): approved content only; national and provincial
content; this school's own school and class content and nothing of any other
school; restricted material (assessment keys) never; teacher-only material
only when the policy includes it (the school server enforces access per role,
as the core does); optionally only some subjects and grades. Nothing about
learners is ever in a package: packages are built from the curriculum index,
which holds no learner data.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
import tarfile
from array import array
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

PACKAGE_SCHEMA = "nexa.edge-package/1.0"
PROBE_TEXTS = (
    "search_query: What enzyme unwinds the DNA double helix?",
    "search_query: How does a transformer use electromagnetic induction?",
    "search_query: What is a covalent bond?",
)


@dataclass
class Policy:
    school_id: str
    province: Optional[str] = None
    include_teacher_only: bool = True
    subjects: list[str] = field(default_factory=list)   # empty = all
    grades: list[int] = field(default_factory=list)     # empty = all
    languages: list[str] = field(default_factory=list)  # empty = all

    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]


def _grades(md: dict[str, Any]) -> set[int]:
    g = md.get("grade_levels")
    vals = g if isinstance(g, (list, tuple)) else str(g or "").split(",")
    out = set()
    for v in vals:
        try:
            out.add(int(str(v).strip()))
        except ValueError:
            pass
    return out


def select(md: dict[str, Any], p: Policy) -> tuple[bool, str]:
    """Whether a chunk belongs in this school's package, and why not."""
    if md.get("approval_status") != "approved":
        return False, "not approved"
    access = md.get("access_classification", "learner")
    if access == "restricted":
        return False, "restricted (assessment material stays on core)"
    if access == "teacher_only" and not p.include_teacher_only:
        return False, "teacher-only excluded by policy"
    scope = md.get("scope", "national")
    if scope in ("school", "class") and md.get("school_id") != p.school_id:
        return False, "another school's content"
    if scope == "provincial" and p.province and md.get("province") not in (None, p.province):
        return False, "another province's content"
    if p.subjects and md.get("subject") not in p.subjects:
        return False, "subject not taught at this school"
    if p.grades and not (_grades(md) & set(p.grades)):
        return False, "grade not taught at this school"
    if p.languages and md.get("language", "en") not in p.languages:
        return False, "language not used at this school"
    return True, ""


# ---------------------------------------------------------------- sqlite layout

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, text TEXT NOT NULL,
  metadata TEXT NOT NULL, vector BLOB NOT NULL, embedding_text TEXT);
CREATE TABLE IF NOT EXISTS local_vectors (chunk_id TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS chunks_version ON chunks(version_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, body, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS removed (version_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def fts_body(text: str, md: dict[str, Any]) -> str:
    return " ".join(x for x in (md.get("title"), md.get("section_path"), text) if x)


def vec_bytes(v: Iterable[float]) -> bytes:
    """float16, little endian: half the size of float32, and the loss is far
    below anything that changes a ranking."""
    import struct
    vals = list(v)
    return struct.pack(f"<{len(vals)}e", *vals)


def vec_from(b: bytes) -> list[float]:
    import struct
    return list(struct.unpack(f"<{len(b) // 2}e", b))


def _canon(md: dict[str, Any]) -> str:
    return json.dumps(md, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def source_hash(rows: list[tuple[str, str, str, bytes]]) -> str:
    """Hash of one source's chunk set: (chunk_id, text, metadata json, vector)."""
    h = hashlib.sha256()
    for cid, text, md, vec in sorted(rows):
        for part in (cid.encode(), text.encode(), md.encode(), vec):
            h.update(len(part).to_bytes(4, "big"))
            h.update(part)
    return h.hexdigest()


def write_db(path: str, items: list[dict[str, Any]], removed: Iterable[str] = ()) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        for it in items:
            md = it["metadata"]
            con.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                        (it["chunk_id"], it["version_id"], it["text"], _canon(md), vec_bytes(it["vector"]),
                         it.get("embedding_text") or it["text"]))
            con.execute("INSERT INTO chunks_fts VALUES (?,?)", (it["chunk_id"], fts_body(it["text"], md)))
        con.executemany("INSERT INTO removed VALUES (?)", [(v,) for v in removed])
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


def content_hash(rows: list[tuple[str, str, str, str]]) -> str:
    """Hash of one source's content without its vectors: (chunk_id, text,
    metadata json, embedding text). A school server that re-embeds with its own
    model (sync.py) still proves the content is exactly the core's."""
    h = hashlib.sha256()
    for row in sorted(rows):
        for part in row:
            b = (part or "").encode()
            h.update(len(b).to_bytes(4, "big"))
            h.update(b)
    return h.hexdigest()


def db_sources(path: str) -> dict[str, dict[str, Any]]:
    """version_id -> {sha256, content_sha256, chunks} for every source in a package database."""
    con = sqlite3.connect(path)
    try:
        rows: dict[str, list] = {}
        crows: dict[str, list] = {}
        for cid, vid, text, md, vec, et in con.execute(
                "SELECT chunk_id, version_id, text, metadata, vector, embedding_text FROM chunks"):
            rows.setdefault(vid, []).append((cid, text, md, bytes(vec)))
            crows.setdefault(vid, []).append((cid, text, md, et or text))
    finally:
        con.close()
    return {v: {"sha256": source_hash(r), "content_sha256": content_hash(crows[v]), "chunks": len(r)}
            for v, r in rows.items()}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sign(manifest_bytes: bytes, key: bytes) -> str:
    return hmac.new(key, manifest_bytes, hashlib.sha256).hexdigest()


def verify(manifest_bytes: bytes, signature: str, key: bytes) -> bool:
    return hmac.compare_digest(sign(manifest_bytes, key), (signature or "").strip())


def school_key(master: bytes, school_id: str) -> bytes:
    """Each school's sync key, derived from the core's master key: a school
    server holds only its own key, so one compromised school cannot forge
    another school's packages."""
    return hmac.new(master, f"nexa-edge-sync:{school_id}".encode(), hashlib.sha256).digest()


# ---------------------------------------------------------------- build (core)

@dataclass
class BuildResult:
    directory: str
    manifest: dict[str, Any]
    kind: str                     # full | delta
    bundle: Optional[str] = None  # .tar path when requested


async def collect(store, index_id: str, policy: Policy, embedding_text_of=None
                  ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every chunk of the index this school may have, with its vector and the
    exact text the core embedded (embedding_text_of(chunk_id), from the
    ingestion registry), so a school server can re-embed identically."""
    ids = sorted(await store.chunk_ids(index_id))
    items, skipped = [], {}
    for i in range(0, len(ids), 500):
        got = await store.get_items(index_id, ids[i:i + 500])
        for cid in ids[i:i + 500]:
            it = got.get(cid)
            if not it:
                continue
            md = it.get("metadata") or {}
            ok, why = select(md, policy)
            if not ok:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            text = it.get("text") or ""
            et = (embedding_text_of(cid) if embedding_text_of else None) or text
            items.append({"chunk_id": cid, "version_id": it.get("version_id") or md.get("version_id") or cid,
                          "text": text, "metadata": md, "vector": it["vector"], "embedding_text": et})
    return items, skipped


def _sources_of(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, list] = {}
    crows: dict[str, list] = {}
    meta: dict[str, dict[str, Any]] = {}
    for it in items:
        rows.setdefault(it["version_id"], []).append(
            (it["chunk_id"], it["text"], _canon(it["metadata"]), vec_bytes(it["vector"])))
        crows.setdefault(it["version_id"], []).append(
            (it["chunk_id"], it["text"], _canon(it["metadata"]), it.get("embedding_text") or it["text"]))
        md = it["metadata"]
        meta.setdefault(it["version_id"], {"title": md.get("title"), "source_type": md.get("source_type"),
                                           "scope": md.get("scope"), "subject": md.get("subject")})
    return {v: {"sha256": source_hash(r), "content_sha256": content_hash(crows[v]), "chunks": len(r), **meta[v]}
            for v, r in rows.items()}


def build(items: list[dict[str, Any]], *, policy: Policy, index: dict[str, Any], version: int, out_root: str,
          key: bytes, probes: list[list[float]], skipped: Optional[dict[str, int]] = None,
          previous: Optional[dict[str, Any]] = None, make_bundle: bool = True) -> list[BuildResult]:
    """Write the full package for `version` and, given the previous manifest,
    a delta from it. Returns [full, delta?]."""
    sources = _sources_of(items)
    created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    base = {
        "schema": PACKAGE_SCHEMA, "school_id": policy.school_id, "version": version, "created_at": created,
        "policy": asdict(policy), "policy_key": policy.key(),
        "index": index, "probe": {"texts": list(PROBE_TEXTS), "vectors": probes},
        "sources": dict(sorted(sources.items())),
        "stats": {"sources": len(sources), "chunks": len(items), "excluded": skipped or {}},
        "contains_learner_data": False,
    }
    results = []
    full_dir = os.path.join(out_root, policy.school_id, f"v{version}", "full")
    os.makedirs(full_dir, exist_ok=True)
    write_db(os.path.join(full_dir, "knowledge.sqlite"), items)
    results.append(_finish(full_dir, {**base, "kind": "full", "base_version": None,
                                      "files": {"knowledge.sqlite": None}}, key, make_bundle))
    if previous and previous.get("school_id") == policy.school_id and \
            previous.get("index", {}).get("fingerprint") == index.get("fingerprint"):
        old = previous.get("sources", {})
        changed = {v for v, s in sources.items() if old.get(v, {}).get("sha256") != s["sha256"]}
        removed = sorted(set(old) - set(sources))
        delta_dir = os.path.join(out_root, policy.school_id, f"v{version}", f"delta-from-v{previous['version']}")
        os.makedirs(delta_dir, exist_ok=True)
        write_db(os.path.join(delta_dir, "delta.sqlite"), [it for it in items if it["version_id"] in changed], removed)
        results.append(_finish(delta_dir, {**base, "kind": "delta", "base_version": previous["version"],
                                           "delta": {"changed": sorted(changed), "removed": removed},
                                           "files": {"delta.sqlite": None}}, key, make_bundle))
    return results


def _finish(d: str, manifest: dict[str, Any], key: bytes, make_bundle: bool) -> BuildResult:
    for name in manifest["files"]:
        p = os.path.join(d, name)
        manifest["files"][name] = {"sha256": file_sha256(p), "bytes": os.path.getsize(p)}
    raw = json.dumps(manifest, indent=1, sort_keys=True).encode()
    with open(os.path.join(d, "manifest.json"), "wb") as fh:
        fh.write(raw)
    with open(os.path.join(d, "manifest.sig"), "w") as fh:
        fh.write(sign(raw, key))
    res = BuildResult(d, manifest, manifest["kind"])
    if make_bundle:
        name = f"nexa-edge-{manifest['school_id']}-v{manifest['version']}-{manifest['kind']}"
        if manifest["kind"] == "delta":
            name += f"-from-v{manifest['base_version']}"
        res.bundle = os.path.join(os.path.dirname(d), name + ".tar.gz")
        with tarfile.open(res.bundle, "w:gz", compresslevel=6) as tar:
            for f in ["manifest.json", "manifest.sig", *manifest["files"]]:
                tar.add(os.path.join(d, f), arcname=f)
    return res


def load_vectors_f16(blob: bytes) -> array:
    a = array("e")
    a.frombytes(blob)
    return a
