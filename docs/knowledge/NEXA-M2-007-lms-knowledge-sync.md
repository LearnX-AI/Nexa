# NEXA-M2-007: EduNeX / LMS Knowledge Synchronisation

Status: Implemented. Live demonstration on the DGX to run (section 8).
Milestone: M2
Aligns with: NEXA-ADR-0005 (identity), NEXA-M2-001 (metadata 1.1.0), NEXA-M2-002 (ingestion), NEXA-M2-005 (scope filtering)
Contract: **`nexa.lms-sync/1.0`** (contract version 1.0.0), JSON Schema at `GET /v1/knowledge/lms/schema`

Implementation:
- NEXA side: `v1/knowledge/lms_sync.py`, registry tables `lms_items` and `lms_events`, endpoints under `/v1/knowledge/lms/`.
- LMS side: Moodle plugin `local_nexa_integration` 2.0, in `v1/integrations/moodle/`.

Tests:
- `v1/tests/test_lms_sync.py` (12 tests).
- `v1/tests/test_moodle_contract.py`: the plugin's PHP mapping, checked against NEXA's own contract model and sync endpoint.
- The plugin's `tests/contract_check.php` and `tests/mapper_test.php`.

Tool: `v1/tools/lms_sync_demo.py`, an end-to-end demonstration against a running service.

**Status against the acceptance criteria (section 9):** all five are met by tests in CI and by the demonstration against a running service with PostgreSQL. The DGX run of the demonstration produces the production evidence.

## 1. Push and pull

| Direction | Endpoint | Purpose |
|---|---|---|
| push | `PUT /v1/knowledge/lms/content/{sha256}` (and `HEAD`) | Upload a file once, addressed by its SHA-256. Resends never re-upload. |
| push | `POST /v1/knowledge/lms/events` | A batch of up to 100 change events. `accepted` means NEXA has stored the event durably and owns it from here, so the LMS may drop it from its outbox. Events are applied in the background, or before replying with `?wait=true`. |
| pull | `POST /v1/knowledge/lms/reconcile` | The LMS sends a course's full list of current items and revisions. NEXA reports what it is missing (`missing_in_nexa`), what it holds at another revision (`revision_mismatch`), and what the LMS no longer has (`not_in_lms`). With `apply: true` it withdraws the last group, which catches deletions the LMS could not send while offline. |
| observe | `GET /v1/knowledge/lms/status`, `GET /v1/knowledge/lms/events[/{id}]` | Counts by state, oldest unapplied event, last applied event, recent failures, each event's attempts and error. |
| retry | `POST /v1/knowledge/lms/events/{id}/retry`, `POST /v1/knowledge/lms/events/retry-failed` | Re-apply failed events. The service also retries failed, retryable events itself every `NEXA_LMS_RETRY_INTERVAL` seconds (default 300). |

**An event:**

```json
{"contract": "nexa.lms-sync/1.0",
 "site": {"system": "moodle", "site_id": "edunex-ncd", "school_id": "pg-ncd-0042"},
 "events": [{
   "event_id": "mdl-3f1c...",                 // generated once by the LMS, reused on every resend
   "op": "upsert",                            // or "delete"
   "occurred_at": "2026-11-01T08:00:00Z",     // when the change happened in the LMS
   "item": {
     "course_id": "17", "item_id": "cm345", "course_name": "Grade 12 Biology", "kind": "page",
     "revision": "1793519000", "title": "Photosynthesis", "url": "https://moodle/mod/page/view.php?id=345",
     "visibility": "students",                // students | teachers | hidden
     "approval": {"status": "approved", "by": "moodle-policy:tag"},   // approved | pending_review | draft | withdrawn
     "scope": "school",                       // school | national
     "curriculum": {"subject": "Biology", "grade_levels": [12], "topic_code": "12.4.1", "language": "en"},
     "supersedes": "cm300",                   // optional: an item this one replaces
     "content": {"format": "html", "body": "<h2>...</h2>"}   // or {"file": {"sha256", "name", "mime", "size"}}
   }}]}
```

**The result for each event:**
- `accepted`;
- `duplicate` (the `event_id` was already received; carries its current `state`);
- `rejected`, with a reason. A rejected event will never succeed as sent, for example a missing grade, a national scope without permission, or an `event_id` reused for another item.

With `wait=true` the result also includes the final `state`: `applied`, `failed`, `rejected` or `stale`.

## 2. Authentication

Each LMS site gets a **sync token**: role `lms_sync`, permission `knowledge.lms.sync` only, with two new identity claims:
- `lms_site`: the site it may write for;
- `school_id`: the school its content belongs to.

`lms_national: true` is an optional third claim for a site that publishes national content. The token is minted with:

```
python -m v1.identity.cli --subject lms:edunex-ncd --roles lms_sync --lms-site edunex-ncd \
    --school-id pg-ncd-0042 --ttl 31536000
```

What the token allows:
- **Bound to its site and school.** A token for one site cannot write another site's content (403). A site's `school_id` that differs from the token's is refused (403). School-scoped content always takes the token's school, never the payload's.
- **Writes only.** A sync token cannot query, upload outside the sync API, or see another site's events.
- **Sent only in the header.** The token travels in the `Authorization` header; the plugin never puts it in a URL or a form field.

## 3. Change detection

Item key: `system:site_id:item_id`. NEXA source: `lms:<site_id>:<item_id>`.

| Change | Detected by | Effect |
|---|---|---|
| **New** | Unknown item key | A new source, version 1. |
| **Changed content** | New content checksum | A new version of the same source. When approved, it supersedes the previous version, which leaves the index. **Every version is kept** in the registry with its status, so the history is preserved. |
| **Changed governance only** (visibility, approval, title, curriculum) with the same bytes | Metadata differs from the stored version | The same version is reprocessed with the new metadata. |
| **Superseded by another item** | `supersedes: <item_id>` on an approved upsert | The other item is withdrawn, recorded as "superseded by ...". |
| **Deleted** | `op: delete`, `approval.status: withdrawn`, or reconcile `not_in_lms` | Every version is withdrawn: removed from all live indexes, kept in the registry with status `withdrawn`. Deleting an unknown or already-deleted item is a no-op that still succeeds. |
| **Restored** after deletion | Upsert of a withdrawn item | Reactivated, and served again if approved. |
| **Out of order** | `occurred_at` older than the last event applied to the item | Recorded as `stale`; changes nothing. Within a batch, events are applied in `occurred_at` order. |
| **Resent** | Known `event_id` | `duplicate`; nothing is reprocessed. |

Identical bytes under two items are stored once (M2-002 de-duplication); the second item points at the first item's version. A shared version is not withdrawn while another active item still uses it.

## 4. Governance: approval, access, ownership

**Approval.** Only items the LMS marks `approved` are indexed. A `pending_review` or `draft` revision of an approved item does **not** replace it: the last approved version keeps serving until the new one is approved, and the item reports "latest revision is pending_review". Revoking approval on unchanged content removes it from the index.

**Visibility to access class:**

| LMS visibility | NEXA access |
|---|---|
| `students` | `learner` |
| `teachers` (restricted by availability conditions NEXA cannot evaluate) | `teacher_only` |
| `hidden` | `teacher_only` |

The plugin never sends private or user-owned files.

**Ownership and versions.** Each record keeps:
- the LMS site, course, item, revision, kind and URL (metadata schema **1.1.0**: optional `site_id`, `kind`, `url` and `owner_ref` on the LMS reference, and the systems `moodle` and `learnx`; 1.0.0 records stay valid);
- the approver reference;
- the course name as publisher;
- every version.

**No personal data.** The plugin sends no user data: approvers are policy references such as `moodle-policy:tag`, not people.

## 5. School-local and national propagation

| Content | Scope in NEXA | Who retrieves it |
|---|---|---|
| A school site's course content (default) | `school`, with `jurisdiction.school_id` from the token | Only users whose token carries the same `school_id` claim, and administrators |
| A national content site (token has `lms_national`), item `scope: national` | `national` | Every user allowed the access class |
| Any upsert with `scope: national` from a token without `lms_national` | — | Rejected |

Authority is unchanged by scope. LMS content is tier 4 (M2-001), so the M2-005 arbitration places official syllabus and guide passages above it where both cover the same point. School content supplements national content of the same tier but never overrides it.

At the school edge (M2-009), an edge serves national content plus its own school's content. These rules are what it filters on.

## 6. Retry, offline queue and observability

**LMS side (the plugin's outbox, `local_nexa_outbox`).**
- **Queued first.** Every change becomes a row before anything is sent, with its event ID generated once.
- **Sending.** `push_outbox` runs every 5 minutes. It uploads a file first if NEXA lacks it (`HEAD`, then `PUT`), then sends the events.
- **Final answers.** A row leaves the queue only on a final answer: accepted, duplicate, stale or rejected.
- **Failures.** On a network error, a NEXA 5xx, or a 401 or 403, the row stays queued with exponential backoff: 1 minute doubling up to 6 hours. A 422 on a batch is retried one event at a time, so one bad event cannot block the rest.
- **Superseded rows.** A newer change to an item replaces its never-attempted queued row.

**Safety net.** A nightly scan (`sync_materials`) queues anything the observers missed and reconciles each course. Connectivity loss only delays delivery.

**NEXA side.** Every event is stored in `lms_events` with its state, attempts, error code and detail, and the ingestion job and version it produced:
- **Failures.** A processing or embedding failure is `failed` and `retryable`; a failure after `NEXA_LMS_MAX_ATTEMPTS` (default 5) needs an operator retry.
- **Watching.** `/v1/knowledge/lms/status` and the `nexa_lms_events_total{state,op}` metric show the backlog, and an audit event is written per applied event.

## 7. The Moodle plugin (2.0)

What it does:
- **Activities sent:** pages, labels, books, and PDF, DOCX or PPTX resources.
- **When:** observers queue changes as they happen, on module created, updated or deleted, course updated or deleted, and the approval tag added or removed.
- **Approval:** the activity tag `nexa-approved` (configurable), or an auto-approved course category.
- **Curriculum:** subject and grade come from the course custom fields `nexa_subject` and `nexa_grade`, otherwise from the course name.
- **Missing grade:** an activity in a course with no grade is not sent, and the reason is recorded.

**Kept from 1.x:**
- **The launcher** is unchanged.
- **The legacy v0 push** survives as `legacy_v0_push`, **disabled by default**. It now sends only activities students can see, and does not re-send files 1.x already sent.

Install and settings are covered in the plugin README.

## 8. Demonstration

`lms_sync_demo.py` acts as an LMS site's sync client, using only the public contract against a running Knowledge Service. It writes only to its own demo site and withdraws everything at the end.

**Local run.** Knowledge Service with PostgreSQL + pgvector and a stand-in embedder: **10 of 10 steps passed** (`m2-007-lms-sync-demo-local.json`).

| Step | Result |
|---|---|
| Approved LMS page retrievable by its school's students, with `lms://` provenance | pass |
| Event for a not-yet-uploaded file fails, visible in status | pass |
| After upload by hash, retry succeeds and the file is retrievable | pass |
| Other schools, and users without a school, cannot see it | pass |
| Changed revision supersedes the old text | pass |
| Pending revision does not replace the approved one | pass |
| Hidden: teachers see it, students do not | pass |
| Stale event ignored; resent event is a duplicate | pass |
| Reconcile withdraws an item deleted while offline | pass |
| Deleted item gone from retrieval; its events kept | pass |

**On the DGX**, against the live service:

```
set -a; source <(sudo cat /etc/nexa/identity.env); set +a
/home/admin/Nexa/venv-v1/bin/python v1/tools/lms_sync_demo.py --base http://127.0.0.1:8103 --out docs/knowledge/evidence
```

## 9. Acceptance criteria (NEXA-M2-007)

| Criterion | Status | Evidence |
|---|---|---|
| Sync contract documented and versioned | **Met** | Sections 1 to 6; `nexa.lms-sync/1.0` with JSON Schema at `/v1/knowledge/lms/schema`; metadata schema 1.1.0 |
| Incremental update and deletion/supersession scenarios tested | **Met** | `test_changed_superseded_pending_hidden_and_deleted`, `test_item_superseding_another_item`, `test_stale_and_duplicate_events`, `test_reconcile_*`; demonstration steps 5 to 10 |
| Access/approval rules enforced | **Met** | `test_token_is_bound_to_its_site_and_school`, `test_approved_lms_page_becomes_retrievable_for_its_school_only`, `test_approval_revoked_with_same_content_removes_it`; the M2-005 filter tests |
| Failed sync is observable and retryable | **Met** | `test_gateway_outage_fails_retryably_then_recovers`, `test_file_content_by_hash_and_missing_content_retry`; status and events endpoints; plugin outbox with backoff |
| End-to-end demonstration from approved LMS content to retrievable NEXA knowledge | **Met locally** (10/10 against a running service with pgvector). DGX run to add. | Section 8; `test_moodle_contract.py` (the plugin's own PHP output applied through the endpoint) |

## 10. Limitations

- **Plugin not yet run in Moodle.** The plugin's Moodle-facing code (observers, tasks, file access) has not been run inside a Moodle instance here. Its mapping rules are tested, and its output is checked against NEXA, but the first install on `moodle2` should be watched: the task logs and `local_nexa_outbox`.
- **Shared content keeps the first governance.** Identical bytes under two items share one version, and that version carries the first item's governance.
- **Learner identity is separate.** The chat launcher's identity hand-off (email in the URL) is a separate fix: Moodle should pass a signed token that NEXA identity verifies, which also supplies the user's `school_id` claim for school-scoped retrieval.
