"""EduNeX / LearnX / Moodle knowledge synchronisation (NEXA-M2-007).

Contract: nexa.lms-sync/1.0 (CONTRACT), documented in
docs/knowledge/NEXA-M2-007-lms-knowledge-sync.md and published as JSON
Schema at GET /v1/knowledge/lms/schema.

Push. The LMS sends batches of events (POST /v1/knowledge/lms/events). Each
event is an upsert or a delete of one LMS item, with an event_id the LMS
generates once and reuses on every resend, and the time the change happened
in the LMS. File content is uploaded first, addressed by its SHA-256
(PUT /v1/knowledge/lms/content/{sha256}), so a resend never re-uploads a file
NEXA already has.

Pull. Reconcile (POST /v1/knowledge/lms/reconcile) gives NEXA the full list
of a course's current items. NEXA reports what it is missing or holds at an
older revision, and withdraws items the LMS no longer has, which catches
deletions the LMS could not send while it was offline.

Change detection, per item (key system:site_id:item_id):
- new: an item NEXA has never seen becomes a new source, lms:<site>:<item>.
- changed: new content (checksum) is a new version of the same source; the
  version history is kept in the registry. A change of visibility or approval
  with the same content updates the same version.
- superseded: a newly approved version supersedes the previous one (the
  registry marks it superseded and it leaves the index). An item can also
  name another item it replaces (supersedes), which is then withdrawn.
- deleted: every version is withdrawn: out of the index, kept as history.
- stale: an event older than the last one applied to its item is recorded
  and ignored, so out-of-order delivery after an outage cannot roll content
  back.

Governance:
- Only items the LMS marks approved are indexed. A pending revision of an
  approved item does not replace it: the last approved version keeps serving
  until the new one is approved.
- Visibility maps to access: visible to students -> learner; hidden from
  students or restricted -> teacher_only. Private and user-owned files are
  never sent by the LMS client.
- Scope: an item is school-scoped to the school bound to the site's sync
  token, so it is retrievable only by that school's users. National scope
  needs a token with the lms_national claim (a national content site).
- The site and school come from the token, not the payload: a site's token
  cannot write another site's or school's content.

Retry and observability: every event is stored with its state (queued,
applied, failed, rejected, stale), attempts and error. Failed retryable events
are retried by the service on an interval and on request; the LMS client
keeps its own outbox and resends until it gets a final state.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any, Literal, Optional
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from v1.identity.principal import Principal
from v1.observability import audit_event, metrics

from . import config
from .indexing import IndexManager
from .ingestion.pipeline import Pipeline, sha256
from .metadata import Subject

log = logging.getLogger("nexa.knowledge.lms")

CONTRACT = "nexa.lms-sync/1.0"
CONTRACT_VERSION = "1.0.0"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SITE = r"^[a-z0-9][a-z0-9._-]{1,63}$"
_SUBJECTS = {s.value.lower(): s.value for s in Subject}
_SUBJECT_ALIASES = {"maths": "Mathematics", "math": "Mathematics", "science": "General Science",
                    "social studies": "Social Science", "ict": "Technology", "design and technology": "Technology"}
_SCOPE_ADMIN_ROLES = {"admin", "platform_admin"}


# ---------------------------------------------------------------- contract

class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: Literal["moodle", "edunex", "learnx", "lms"]
    site_id: str = Field(..., pattern=_SITE)
    school_id: Optional[str] = Field(None, description="Informational; NEXA uses the token's school claim")


class FileRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    name: str = Field(..., min_length=1, max_length=255)
    mime: Optional[str] = None
    size: int = Field(..., ge=1)


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Optional[Literal["html", "text"]] = None
    body: Optional[str] = None
    file: Optional[FileRef] = None

    @model_validator(mode="after")
    def _one(self) -> "Content":
        if (self.body is None) == (self.file is None):
            raise ValueError("content needs exactly one of body or file")
        if self.body is not None and not self.format:
            self.format = "html"
        return self


class Curriculum(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: Optional[str] = None
    grade_levels: list[int] = Field(default_factory=list)
    term: Optional[int] = Field(None, ge=1, le=4)
    module_code: Optional[str] = None
    topic_code: Optional[str] = None
    outcome_codes: list[str] = Field(default_factory=list)
    language: str = "en"


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["approved", "pending_review", "draft", "withdrawn"]
    by: Optional[str] = Field(None, description="Opaque reference to the approver, never a name or email")
    at: Optional[dt.datetime] = None


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    course_id: str = Field(..., pattern=_ID)
    item_id: str = Field(..., pattern=_ID)
    course_name: Optional[str] = None
    kind: Optional[str] = None
    revision: Optional[str] = Field(None, max_length=128)
    title: Optional[str] = None
    url: Optional[str] = None
    owner_ref: Optional[str] = None
    visibility: Literal["students", "teachers", "hidden"] = "students"
    approval: Optional[Approval] = None
    scope: Literal["school", "national"] = "school"
    curriculum: Curriculum = Field(default_factory=Curriculum)
    effective_from: Optional[dt.date] = None
    effective_to: Optional[dt.date] = None
    supersedes: Optional[str] = Field(None, pattern=_ID, description="item_id this item replaces")
    content: Optional[Content] = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        if v and re.search(r"(?i)(token|secret|password|api[_-]?key|sesskey|sig)=", v):
            raise ValueError("url must not carry credentials or session keys")
        return v


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(..., pattern=r"^[A-Za-z0-9._:-]{8,128}$")
    op: Literal["upsert", "delete"]
    occurred_at: dt.datetime
    item: Item

    @model_validator(mode="after")
    def _needs(self) -> "Event":
        if self.op == "upsert":
            missing = [k for k in ("content", "approval", "title") if getattr(self.item, k) in (None, "")]
            if missing:
                raise ValueError(f"upsert needs item.{', item.'.join(missing)}")
        return self


class SyncBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: Literal["nexa.lms-sync/1.0"]
    site: Site
    events: list[Event] = Field(..., min_length=1)


class ReconcileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(..., pattern=_ID)
    revision: Optional[str] = None


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: Literal["nexa.lms-sync/1.0"]
    site: Site
    course_id: str = Field(..., pattern=_ID)
    items: list[ReconcileItem]
    apply: bool = False


def contract_schema() -> dict[str, Any]:
    return {
        "$id": f"https://nexa.learnx.ai/schemas/lms-sync/{CONTRACT_VERSION}.json",
        "title": f"NEXA LMS sync contract {CONTRACT}",
        "events_request": SyncBatch.model_json_schema(),
        "reconcile_request": ReconcileRequest.model_json_schema(),
    }


class SyncError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- mapping

def item_key(site: Site, item_id: str) -> str:
    return f"{site.system}:{site.site_id}:{item_id}"


def source_id_for(site: Site, item_id: str) -> str:
    return re.sub(r"[^a-z0-9._:-]", "-", f"lms:{site.site_id}:{item_id}".lower())[:128]


def _subject(name: Optional[str]) -> str:
    n = (name or "").strip().lower()
    return _SUBJECTS.get(n) or _SUBJECT_ALIASES.get(n) or "General"


class Rejected(Exception):
    """The event can never succeed as sent; resending it unchanged will not help."""


def to_metadata(site: Site, school_id: Optional[str], ev: Event) -> dict[str, Any]:
    it = ev.item
    if not it.curriculum.grade_levels:
        raise Rejected("item.curriculum.grade_levels is required (derive it from the course)")
    title = (it.title or "").strip()
    if len(title) < 3:
        raise Rejected("item.title must be at least 3 characters")
    if it.scope == "school" and not school_id:
        raise Rejected("school-scoped content needs a sync token with a school_id claim")
    effective = it.effective_from or (it.approval.at.date() if it.approval and it.approval.at
                                      else ev.occurred_at.date())
    uri = (f"lms://{quote(site.site_id)}/course/{quote(it.course_id, safe='')}/item/{quote(it.item_id, safe='')}"
           + (f"?rev={quote(it.revision, safe='')}" if it.revision else ""))
    md: dict[str, Any] = {
        "source_id": source_id_for(site, it.item_id),
        "source_type": "lms_content",
        "title": title[:300],
        "subject": _subject(it.curriculum.subject),
        "grade_levels": it.curriculum.grade_levels,
        "language": it.curriculum.language,
        "scope": it.scope,
        "jurisdiction": {"country": "PG", "school_id": school_id} if it.scope == "school" else {"country": "PG"},
        "approval_status": it.approval.status if it.approval else "pending_review",
        "approved_by": it.approval.by if it.approval else None,
        "access_classification": "learner" if it.visibility == "students" else "teacher_only",
        "effective_from": effective.isoformat(),
        "effective_to": it.effective_to.isoformat() if it.effective_to else None,
        "source_uri": uri,
        "publisher": it.course_name,
        "lms": {"system": site.system, "site_id": site.site_id, "course_id": it.course_id, "item_id": it.item_id,
                "revision": it.revision, "kind": it.kind, "url": it.url, "owner_ref": it.owner_ref},
        "tags": ["lms", f"course:{it.course_id}"],
        "curriculum": {k: v for k, v in {"module_code": it.curriculum.module_code,
                                         "topic_code": it.curriculum.topic_code,
                                         "outcome_codes": it.curriculum.outcome_codes}.items() if v},
    }
    if it.curriculum.term:
        md["term"] = it.curriculum.term
    return {k: v for k, v in md.items() if v is not None}


_GOVERNANCE = ("approval_status", "approved_by", "access_classification", "scope", "effective_from",
               "effective_to", "title", "subject", "grade_levels", "language", "term", "source_uri", "publisher")


def governance_differs(stored: dict[str, Any], md: dict[str, Any]) -> bool:
    """True when the registry's metadata for a version is not what this event
    says it should be."""
    def norm(v):
        return sorted(v) if isinstance(v, list) else (None if v in ("", None) else str(v))
    if any(norm(stored.get(k)) != norm(md.get(k)) for k in _GOVERNANCE):
        return True
    if (stored.get("jurisdiction") or {}).get("school_id") != (md.get("jurisdiction") or {}).get("school_id"):
        return True
    a, b = stored.get("lms") or {}, md.get("lms") or {}
    if any(norm(a.get(k)) != norm(b.get(k)) for k in ("revision", "kind", "url", "course_id", "owner_ref")):
        return True
    return (stored.get("curriculum") or {}) != {**(stored.get("curriculum") or {}), **(md.get("curriculum") or {})}


# ---------------------------------------------------------------- service

class LmsSync:
    def __init__(self, pipeline: Pipeline, index: IndexManager) -> None:
        self.pipe = pipeline
        self.reg = pipeline.registry
        self.index = index
        self._lock = asyncio.Lock()

    # ---- authorisation

    @staticmethod
    def authorise(principal: Principal, site: Site) -> Optional[str]:
        """Check the token may write for this site. Returns the school_id that
        school-scoped content from this site belongs to."""
        admin = bool(set(principal.roles) & _SCOPE_ADMIN_ROLES)
        if not admin:
            bound = principal.attrs.get("lms_site")
            if not bound or bound != site.site_id:
                raise SyncError(403, "this token is not bound to that LMS site")
            if site.school_id and principal.school_id and site.school_id != principal.school_id:
                raise SyncError(403, "site.school_id does not match the token's school")
            return principal.school_id
        return principal.school_id or site.school_id

    @staticmethod
    def may_publish_national(principal: Principal) -> bool:
        return bool(set(principal.roles) & _SCOPE_ADMIN_ROLES) or principal.attrs.get("lms_national") is True

    # ---- receive

    def receive(self, batch: SyncBatch, principal: Principal) -> tuple[list[dict[str, Any]], list[str]]:
        """Record a batch. Returns per-event results and the event ids to apply."""
        if len(batch.events) > config.LMS_MAX_BATCH:
            raise SyncError(413, f"at most {config.LMS_MAX_BATCH} events per request")
        school = self.authorise(principal, batch.site)
        national_ok = self.may_publish_national(principal)
        results, todo = [], []
        for ev in sorted(batch.events, key=lambda e: e.occurred_at):
            key = item_key(batch.site, ev.item.item_id)
            prior = self.reg.get_lms_event(ev.event_id)
            if prior:
                if prior["item_key"] != key:
                    results.append({"event_id": ev.event_id, "status": "rejected", "item_key": key,
                                    "reason": "event_id was already used for another item"})
                else:
                    results.append({"event_id": ev.event_id, "status": "duplicate", "item_key": key,
                                    "state": prior["state"]})
                continue
            payload = {"site": batch.site.model_dump(mode="json"), "school_id": school,
                       "event": ev.model_dump(mode="json")}
            reason = None
            if ev.op == "upsert" and ev.item.scope == "national" and not national_ok:
                reason = "national scope needs a sync token with the lms_national claim"
            elif ev.op == "upsert":
                try:
                    to_metadata(batch.site, school, ev)
                except (Rejected, ValueError) as exc:
                    reason = str(exc)
            occurred = ev.occurred_at.astimezone(dt.timezone.utc).isoformat()
            if reason:
                self.reg.add_lms_event(ev.event_id, key, ev.op, occurred, payload, principal.subject,
                                       state="rejected")
                self.reg.update_lms_event(ev.event_id, error_code="rejected", error_detail=reason[:500])
                metrics.inc("nexa_lms_events_total", state="rejected", op=ev.op)
                results.append({"event_id": ev.event_id, "status": "rejected", "item_key": key, "reason": reason})
                continue
            self.reg.add_lms_event(ev.event_id, key, ev.op, occurred, payload, principal.subject)
            metrics.inc("nexa_lms_events_total", state="received", op=ev.op)
            results.append({"event_id": ev.event_id, "status": "accepted", "item_key": key})
            todo.append(ev.event_id)
        return results, todo

    # ---- apply

    async def apply_many(self, event_ids: list[str], trace_id: Optional[str] = None) -> list[dict[str, Any]]:
        out = []
        for eid in event_ids:
            out.append(await self.apply(eid, trace_id))
        return out

    async def apply(self, event_id: str, trace_id: Optional[str] = None) -> dict[str, Any]:
        async with self._lock:
            e = self.reg.get_lms_event(event_id)
            if e is None:
                raise SyncError(404, "unknown event")
            if e["state"] in ("applied", "rejected", "stale"):
                return e
            self.reg.update_lms_event(event_id, state="queued", attempts=e["attempts"] + 1)
            try:
                result = await self._apply(e, trace_id)
            except Rejected as exc:
                result = self.reg.update_lms_event(event_id, state="rejected", retryable=False,
                                                   error_code="rejected", error_detail=str(exc)[:500])
            except Exception as exc:  # noqa: BLE001 - recorded on the event for retry
                log.warning("lms event %s failed: %s", event_id, exc)
                result = self.reg.update_lms_event(event_id, state="failed", retryable=True,
                                                   error_code=type(exc).__name__, error_detail=str(exc)[:500])
            if result["state"] == "failed" and result["attempts"] >= config.LMS_MAX_ATTEMPTS:
                result = self.reg.update_lms_event(event_id, retryable=False)
            metrics.inc("nexa_lms_events_total", state=result["state"], op=result["op"])
            audit_event(action=f"knowledge.lms.{result['op']}", decision=result["state"],
                        principal=result.get("principal") or "lms", trace_id=trace_id,
                        item=result["item_key"], lms_event=event_id)
            return result

    async def _apply(self, e: dict[str, Any], trace_id: Optional[str]) -> dict[str, Any]:
        site = Site(**e["payload"]["site"])
        ev = Event(**e["payload"]["event"])
        school = e["payload"].get("school_id")
        key = e["item_key"]
        item = self.reg.get_lms_item(key)
        if item and item.get("last_event_at") and e["occurred_at"] < item["last_event_at"]:
            return self.reg.update_lms_event(
                e["event_id"], state="stale", retryable=False, error_code="stale",
                error_detail=f"an event from {item['last_event_at']} was already applied to this item")
        if ev.op == "delete" or (ev.item.approval and ev.item.approval.status == "withdrawn"):
            await self._withdraw(key, site, ev.item.item_id, e)
            return self.reg.update_lms_event(e["event_id"], state="applied", retryable=False,
                                             error_code=None, error_detail=None)

        md = to_metadata(site, school, ev)
        content = ev.item.content
        assert content is not None
        if content.file is not None:
            data = self.pipe.blobs.get("sha256:" + content.file.sha256)
            if data is None:
                raise RuntimeError(f"content {content.file.sha256} has not been uploaded "
                                   "(PUT /v1/knowledge/lms/content/{sha256} first)")
            job, _ = await run_in_threadpool(self.pipe.submit, content.file.name, data, {"metadata": md},
                                             e.get("principal"))
        else:
            job, _ = await run_in_threadpool(self.pipe.submit_lms, {
                "lms": md["lms"], "title": md["title"], "body": content.body, "format": content.format,
                "metadata": md}, e.get("principal"))
        if job["state"] in ("queued", "running") or job["stage"] not in ("index", "done"):
            job = await run_in_threadpool(self.pipe.process, job["job_id"])
        elif job.get("version_id") and job["state"] != "duplicate":
            # An identical earlier job was reused (same bytes, same manifest). The
            # registry holds the governance of whichever event was processed
            # last, which may be a different one (hidden, then visible again):
            # reprocess with this event's metadata if it differs.
            cur = self.reg.get_source(job["version_id"])
            if cur and governance_differs(cur["metadata"], md):
                job = await run_in_threadpool(self.pipe.retry, job["job_id"], md)
        self.reg.update_lms_event(e["event_id"], job_id=job["job_id"])
        if job["state"] == "quarantined" or (job["state"] == "failed" and not job["retryable"]):
            raise Rejected(f"ingestion {job['state']}: {job.get('error_code')}: {job.get('error_detail') or ''}"[:400])
        if job["state"] == "failed":
            raise RuntimeError(f"ingestion failed at {job['stage']}: {job.get('error_code')}")
        # A duplicate job means the same bytes are already registered under
        # another source; they are stored once and this item points at that
        # version (see _withdraw for how shared versions are kept).
        vid = job["version_id"]
        src = self.reg.get_source(vid)
        if src and src["status"] == "withdrawn":
            self.reg.reactivate_source(vid)
            if md["approval_status"] == "approved":
                self.reg.supersede_older(src["source_id"], vid)
        approved = md["approval_status"] == "approved"
        if approved:
            await self.index.index_version(vid, trace_id)
            state = "active"
        else:
            # Not (or no longer) approved: this version must not be served. An
            # older approved version of the item, if any, keeps serving.
            await self.index.remove_version(vid)
            served = self._has_served_version(src["source_id"] if src else md["source_id"], vid)
            state = "active" if served else "pending"
        if approved and ev.item.supersedes:
            old_key = item_key(site, ev.item.supersedes)
            if old_key != key and self.reg.get_lms_item(old_key):
                await self._withdraw(old_key, site, ev.item.supersedes, e, superseded_by=key)
        self.reg.put_lms_item(key, system=site.system, site_id=site.site_id, course_id=ev.item.course_id,
                              item_id=ev.item.item_id, source_id=md["source_id"], state=state,
                              revision=ev.item.revision, checksum=job["checksum"], version_id=vid,
                              last_event_id=e["event_id"], last_event_at=e["occurred_at"],
                              detail=None if approved else f"latest revision is {md['approval_status']}")
        return self.reg.update_lms_event(e["event_id"], state="applied", retryable=False, version_id=vid,
                                         error_code=None, error_detail=None)

    def _has_served_version(self, source_id: str, except_vid: str) -> bool:
        return any(v["status"] == "active" and v["version_id"] != except_vid
                   and v["metadata"].get("approval_status") == "approved"
                   for v in self.reg.versions_of(source_id))

    async def _withdraw(self, key: str, site: Site, item_id: str, e: dict[str, Any],
                        superseded_by: Optional[str] = None) -> None:
        item = self.reg.get_lms_item(key)
        source_id = item["source_id"] if item else source_id_for(site, item_id)
        shared = {i["version_id"] for i in self.reg.lms_items_of(site.system, site.site_id)
                  if i["item_key"] != key and i["state"] == "active" and i.get("version_id")}
        for v in self.reg.versions_of(source_id):
            if v["version_id"] not in shared:
                await self.index.remove_version(v["version_id"])
        # Also an item pointing at another item's (duplicate) version.
        if item and item.get("version_id") and item["version_id"] not in shared:
            other = self.reg.get_source(item["version_id"])
            if other and other["source_id"] != source_id:
                await self.index.remove_version(item["version_id"])
        self.reg.withdraw_source(source_id)
        if item or superseded_by is None:
            self.reg.put_lms_item(
                key, system=site.system, site_id=site.site_id,
                course_id=item["course_id"] if item else e["payload"]["event"]["item"]["course_id"],
                item_id=item_id, source_id=source_id, state="withdrawn",
                last_event_id=e["event_id"],
                last_event_at=max(e["occurred_at"], (item or {}).get("last_event_at") or ""),
                detail=f"superseded by {superseded_by}" if superseded_by else "deleted in the LMS")

    # ---- retry

    async def retry(self, event_id: str, trace_id: Optional[str] = None) -> dict[str, Any]:
        e = self.reg.get_lms_event(event_id)
        if e is None:
            raise SyncError(404, "unknown event")
        if e["state"] != "failed":
            raise SyncError(409, f"event is {e['state']}; only failed events can be retried")
        if not e["retryable"]:
            self.reg.update_lms_event(event_id, retryable=True, attempts=0)
        return await self.apply(event_id, trace_id)

    async def retry_failed(self, trace_id: Optional[str] = None) -> dict[str, int]:
        done = {"retried": 0, "applied": 0, "failed": 0}
        for e in self.reg.list_lms_events("failed", limit=500):
            if not e["retryable"]:
                continue
            r = await self.apply(e["event_id"], trace_id)
            done["retried"] += 1
            done["applied" if r["state"] == "applied" else "failed"] += 1
        return done

    # ---- reconcile

    async def reconcile(self, req: ReconcileRequest, principal: Principal,
                        trace_id: Optional[str] = None) -> dict[str, Any]:
        self.authorise(principal, req.site)
        have = {i["item_id"]: i for i in self.reg.lms_items_of(req.site.system, req.site.site_id, req.course_id)
                if i["state"] != "withdrawn"}
        lms = {i.item_id: i for i in req.items}
        missing = sorted(i for i in lms if i not in have and
                         not (self.reg.get_lms_item(item_key(req.site, i)) or {}).get("state") == "rejected")
        mismatch = sorted(i for i in lms if i in have and lms[i].revision and have[i].get("revision")
                          and lms[i].revision != have[i]["revision"])
        gone = sorted(i for i in have if i not in lms)
        withdrawn = []
        if req.apply:
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            for item_id in gone:
                key = item_key(req.site, item_id)
                eid = f"reconcile:{key}:{now}"[:128]
                payload = {"site": req.site.model_dump(mode="json"), "school_id": None,
                           "event": {"event_id": re.sub(r"[^A-Za-z0-9._:-]", "-", eid), "op": "delete",
                                     "occurred_at": now,
                                     "item": {"course_id": req.course_id, "item_id": item_id}}}
                ev_id = payload["event"]["event_id"]
                self.reg.add_lms_event(ev_id, key, "delete", now, payload, principal.subject)
                r = await self.apply(ev_id, trace_id)
                if r["state"] == "applied":
                    withdrawn.append(item_id)
        return {"contract": CONTRACT, "course_id": req.course_id, "lms_items": len(lms), "nexa_items": len(have),
                "missing_in_nexa": missing, "revision_mismatch": mismatch, "not_in_lms": gone,
                "withdrawn": withdrawn, "in_sync": not (missing or mismatch or (gone and not req.apply))}

    # ---- content

    def put_content(self, digest: str, data: bytes) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SyncError(422, "content must be addressed by its lowercase hex SHA-256")
        if len(data) > config.MAX_FILE_BYTES:
            raise SyncError(413, "file exceeds size limit")
        if sha256(data) != "sha256:" + digest:
            raise SyncError(422, "content does not match its SHA-256")
        existed = self.pipe.blobs.get("sha256:" + digest) is not None
        if not existed:
            self.pipe.blobs.put("sha256:" + digest, data)
        return {"sha256": digest, "size": len(data), "stored": not existed}

    def has_content(self, digest: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and self.pipe.blobs.get("sha256:" + digest) is not None

    def status(self) -> dict[str, Any]:
        c = self.reg.lms_counts()
        failed = self.reg.list_lms_events("failed", limit=20)
        return {"contract": CONTRACT, **c,
                "recent_failures": [{k: f.get(k) for k in ("event_id", "item_key", "op", "attempts", "retryable",
                                                           "error_code", "error_detail", "updated_at")}
                                    for f in failed]}
