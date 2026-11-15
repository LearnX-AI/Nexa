"""HTTP side of NEXA-M2-009, for both roles of the knowledge service.

Core NEXA (NEXA_EDGE_PACKAGE_DIR set): serves each school's packages to its
school server, and accepts answer requests a school server forwards on behalf
of a student or teacher.

    GET  /v1/edge/packages/{school}/latest            latest manifest
    GET  /v1/edge/packages/{school}/{version}/bundle  ?from=N: delta if one
                                                       exists from N, else full
    POST /v1/edge/reports                              sync and usage counts

School server (NEXA_EDGE_MODE=true):
- read-only: requests that would change content are refused (409); content
  changes are made on core NEXA and arrive in packages;
- /v1/knowledge/lms/* is passed through to core NEXA unchanged (the school's
  Moodle may point at its school server); when the core is unreachable the
  school server answers 503 and the Moodle plugin keeps the event in its
  outbox and retries (NEXA-M2-007);
- answers: retrieval is always local. Generation goes to core NEXA when
  NEXA_EDGE_ANSWER_ROUTE=core_first and the core answers in time, else to the
  local model; repeated questions are answered from a cache keyed by package
  version, role and question;
    GET  /v1/edge/status   package, sync history, routing, cache
    POST /v1/edge/sync     pull now (admin)

Data minimisation: a forwarded answer request carries the question, the
asker's role and the school, never the learner's identity; reports carry
counts only.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import time
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..identity.principal import Principal
from ..knowledge import config

ACT_AS_HEADER = "X-NEXA-Act-As-Role"
DELEGABLE = ("student", "teacher")
READ_PATHS = ("/v1/knowledge/query", "/v1/knowledge/answer", "/v1/edge/sync")


# ---------------------------------------------------------------- core side

def _school_allowed(principal: Principal, school: str) -> bool:
    if "edge_node" in principal.roles:
        return principal.school_id == school
    return bool({"admin", "platform_admin"} & set(principal.roles))


def _latest_dir(school: str) -> Optional[str]:
    root = os.path.join(config.EDGE_PACKAGE_DIR, school)
    try:
        versions = sorted(int(n[1:]) for n in os.listdir(root) if n.startswith("v") and n[1:].isdigit())
    except OSError:
        return None
    for v in reversed(versions):
        d = os.path.join(root, f"v{v}", "full")
        if os.path.exists(os.path.join(d, "manifest.json")):
            return d
    return None


def effective_principal(principal: Principal, request: Request) -> Principal:
    """A school server answering on behalf of one of its users: the request is
    authorised as that role at that school, never more."""
    if "edge_node" not in principal.roles:
        if config.EDGE_MODE and config.EDGE_SCHOOL_ID and principal.school_id != config.EDGE_SCHOOL_ID and \
                not ({"admin", "platform_admin"} & set(principal.roles)):
            # A school server serves one school: its users are that school's.
            return Principal(subject=principal.subject, roles=principal.roles, token_type=principal.token_type,
                             school_id=config.EDGE_SCHOOL_ID, attrs=principal.attrs)
        return principal
    role = request.headers.get(ACT_AS_HEADER, "student")
    if role not in DELEGABLE:
        raise HTTPException(status_code=403, detail=f"a school server may act only as {', '.join(DELEGABLE)}")
    return Principal(subject=f"edge:{principal.school_id}:{role}", roles=(role,), token_type="user",
                     school_id=principal.school_id, attrs={"via_edge": principal.subject})


def install_core(app, require_principal) -> None:
    from fastapi import Depends

    @app.get("/v1/edge/packages/{school}/latest")
    async def edge_latest(school: str, principal: Principal = Depends(require_principal)):
        if not _school_allowed(principal, school):
            raise HTTPException(status_code=403, detail="Not permitted")
        d = _latest_dir(school)
        if not d:
            raise HTTPException(status_code=404, detail=f"no package built for school {school}")
        with open(os.path.join(d, "manifest.json")) as fh:
            m = json.load(fh)
        return {k: m[k] for k in ("school_id", "version", "created_at", "index", "stats", "policy_key")}

    @app.get("/v1/edge/packages/{school}/{version}/bundle")
    async def edge_bundle(school: str, version: int, request: Request,
                          principal: Principal = Depends(require_principal)):
        if not _school_allowed(principal, school):
            raise HTTPException(status_code=403, detail="Not permitted")
        vd = os.path.join(config.EDGE_PACKAGE_DIR, school, f"v{version}")
        frm = request.query_params.get("from")
        names = [f"nexa-edge-{school}-v{version}-delta-from-v{frm}.tar.gz"] if frm else []
        names.append(f"nexa-edge-{school}-v{version}-full.tar.gz")
        for n in names:
            p = os.path.join(vd, n)
            if os.path.exists(p):
                return FileResponse(p, media_type="application/gzip", filename=n)
        raise HTTPException(status_code=404, detail="no such package")

    @app.post("/v1/edge/reports")
    async def edge_report(request: Request, principal: Principal = Depends(require_principal)):
        if "edge_node" not in principal.roles:
            raise HTTPException(status_code=403, detail="Not permitted")
        body = await request.json()
        allowed = {"version", "answers", "routes", "cache_hits", "statuses", "sync", "period"}
        rec = {k: v for k, v in body.items() if k in allowed}   # counts only: nothing else is kept
        rec.update(school_id=principal.school_id, received_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        os.makedirs(config.EDGE_PACKAGE_DIR, exist_ok=True)
        with open(os.path.join(config.EDGE_PACKAGE_DIR, "reports.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return {"stored": True}


# ---------------------------------------------------------------- school side

class AnswerCache:
    def __init__(self, size: int, ttl_s: int):
        self.size, self.ttl = size, ttl_s
        self._d: "collections.OrderedDict[str, tuple[float, dict]]" = collections.OrderedDict()
        self.hits = self.misses = 0

    @staticmethod
    def key(version: Optional[int], principal: Principal, question: str, general: Optional[bool]) -> str:
        q = " ".join(re.findall(r"[a-z0-9]+", question.lower()))
        role = "teacher" if {"teacher", "school_admin"} & set(principal.roles) else (
            "admin" if {"admin", "platform_admin"} & set(principal.roles) else "student")
        return hashlib.sha256(f"{version}|{role}|{principal.school_id}|{general}|{q}".encode()).hexdigest()

    def get(self, k: str) -> Optional[dict]:
        hit = self._d.get(k)
        if hit and time.time() - hit[0] < self.ttl:
            self._d.move_to_end(k)
            self.hits += 1
            return hit[1]
        self.misses += 1
        return None

    def put(self, k: str, body: dict) -> None:
        if body.get("status") in ("generation_unavailable", "clarification_needed"):
            return
        self._d[k] = (time.time(), body)
        self._d.move_to_end(k)
        while len(self._d) > self.size:
            self._d.popitem(last=False)


CACHE = AnswerCache(config.EDGE_ANSWER_CACHE_SIZE, config.EDGE_ANSWER_CACHE_TTL_S)
COUNTS: dict[str, collections.Counter] = {"routes": collections.Counter(), "statuses": collections.Counter()}


async def forward_answer(body: dict[str, Any], principal: Principal, trace_id: str) -> Optional[dict]:
    """Ask core NEXA to write the answer (its larger model). None when the core
    is unreachable, slow or refuses: the caller answers locally."""
    if config.EDGE_ANSWER_ROUTE != "core_first" or not (config.EDGE_CORE_URL and config.EDGE_CORE_TOKEN):
        return None
    import httpx
    role = "teacher" if {"teacher", "school_admin"} & set(principal.roles) else "student"
    try:
        async with httpx.AsyncClient(timeout=config.EDGE_CORE_TIMEOUT_S) as c:
            r = await c.post(config.EDGE_CORE_URL.rstrip("/") + "/v1/knowledge/answer", json=body,
                             headers={"Authorization": f"Bearer {config.EDGE_CORE_TOKEN}", ACT_AS_HEADER: role,
                                      "X-NEXA-Trace-Id": trace_id})
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError:
        pass
    return None


def install_edge(app, store_of) -> None:
    """School-server behaviour. Installed always, active only while
    config.EDGE_MODE is true (checked per request)."""
    from fastapi import Depends

    from ..knowledge.auth import require_principal
    from ..identity.rbac import can

    @app.middleware("http")
    async def _school_node(request: Request, call_next):
        path = request.url.path
        if not config.EDGE_MODE:
            if path.startswith(("/v1/edge/status", "/v1/edge/sync")):
                return JSONResponse(status_code=404, content={"detail": "not a school node"})
            return await call_next(request)
        if path.startswith("/v1/knowledge/lms/"):
            return await _proxy_lms(request)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/v1/") and \
                not path.startswith(READ_PATHS):
            return JSONResponse(status_code=409, content={
                "detail": "read-only school node: content and indexes are changed on core NEXA and arrive "
                          "in this school's knowledge package"})
        return await call_next(request)

    @app.on_event("startup")
    async def _edge_sync_loop():
        import asyncio
        if config.EDGE_MODE and config.EDGE_CORE_URL and config.EDGE_CORE_TOKEN and config.EDGE_SYNC_INTERVAL_S > 0:
            asyncio.get_running_loop().create_task(_sync_forever())

    @app.get("/v1/edge/status")
    async def edge_status(principal: Principal = Depends(require_principal)):
        if not can(principal, "knowledge.query"):
            raise HTTPException(status_code=403, detail="Not permitted")
        from .sync import history
        st = store_of()
        m = (st.manifest() if hasattr(st, "manifest") else None) or {}
        return {"school_id": config.EDGE_SCHOOL_ID, "package": {k: m.get(k) for k in
                                                                ("version", "created_at", "stats", "index")},
                "answer_route": config.EDGE_ANSWER_ROUTE, "core_url": config.EDGE_CORE_URL or None,
                "cache": {"entries": len(CACHE._d), "hits": CACHE.hits, "misses": CACHE.misses},
                "routes": dict(COUNTS["routes"]), "statuses": dict(COUNTS["statuses"]),
                "sync": history(config.EDGE_HOME, 10)}

    @app.post("/v1/edge/sync")
    async def edge_sync(principal: Principal = Depends(require_principal)):
        if not ({"admin", "platform_admin", "school_admin"} & set(principal.roles)):
            raise HTTPException(status_code=403, detail="Not permitted")
        from .sync import SyncError, pull
        try:
            got = await pull(config.EDGE_HOME, config.EDGE_CORE_URL, config.EDGE_CORE_TOKEN, sync_key(),
                             config.EDGE_SCHOOL_ID, probe=embed_probe)
        except SyncError as exc:
            return {"installed": None, "detail": str(exc)}
        return {"installed": got.__dict__ if got else None, "detail": "installed" if got else "up to date"}


async def _proxy_lms(request: Request) -> Response:
    import httpx
    if not config.EDGE_CORE_URL:
        return JSONResponse(status_code=503, content={"detail": "core NEXA not configured on this school node"})
    url = config.EDGE_CORE_URL.rstrip("/") + request.url.path
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("authorization", "content-type",
                                                                          "x-nexa-trace-id")}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.request(request.method, url, params=dict(request.query_params), headers=headers,
                                content=await request.body())
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=503, headers={"Retry-After": "300"}, content={
            "detail": f"core NEXA not reachable from this school ({type(exc).__name__}); keep the event and retry"})
    return Response(content=r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


def sync_key() -> bytes:
    k = config.EDGE_SYNC_KEY
    if not k:
        raise RuntimeError("NEXA_EDGE_SYNC_KEY is not set")
    return bytes.fromhex(k) if re.fullmatch(r"[0-9a-fA-F]{64}", k) else k.encode()


async def embed_probe(texts: list[str]) -> list[list[float]]:
    """Embed the package's probe texts with the local model, raw (the texts
    already carry their prefix)."""
    from ..knowledge import embeddings
    vecs, _ = await embeddings._gateway(texts, config.EMBED_MODEL)
    return vecs


async def _sync_forever() -> None:
    """Pull on a schedule; a failed pull (offline, bad package) changes nothing
    and is retried next time. Sends a counts-only report after each pull."""
    import asyncio

    from .sync import SyncError, pull
    while True:
        try:
            await pull(config.EDGE_HOME, config.EDGE_CORE_URL, config.EDGE_CORE_TOKEN, sync_key(),
                       config.EDGE_SCHOOL_ID, probe=embed_probe)
            await send_report()
        except (SyncError, RuntimeError):
            pass
        except Exception:  # noqa: BLE001 - never let the loop die
            pass
        await asyncio.sleep(config.EDGE_SYNC_INTERVAL_S)


async def send_report() -> None:
    import httpx
    from .edgestore import current_version
    body = {"version": current_version(config.EDGE_HOME), "routes": dict(COUNTS["routes"]),
            "statuses": dict(COUNTS["statuses"]), "cache_hits": CACHE.hits}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(config.EDGE_CORE_URL.rstrip("/") + "/v1/edge/reports", json=body,
                         headers={"Authorization": f"Bearer {config.EDGE_CORE_TOKEN}"})
    except httpx.HTTPError:
        pass
