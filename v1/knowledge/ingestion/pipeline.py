"""Controlled ingestion pipeline (NEXA-M2-002), wiring M2-001 and M2-003.

Stages, each recorded on the job so a failure says where it happened:
  validate -> extract -> scan -> metadata -> chunk -> index -> done

submit() validates and stores the original, then returns the job. Submitting
the same bytes with the same manifest and pipeline versions returns the
existing job (idempotent). process() runs extraction to chunking. Indexing
(embedding through the Model Gateway) is done by the service, so the pipeline
stays synchronous, network-free and testable.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections import Counter
from typing import Any, Optional

from pydantic import ValidationError as PydanticValidationError

from .. import config
from ..curriculum import draft_from_filename
from ..metadata import ApprovalStatus, Locator, SourceMetadata
from ..processing.chunking import (CHUNKER_VERSION, STRATEGY, ChunkingConfig, PositionedBlock, chunk_blocks,
                                   profile_for_subject)
from ..processing.normalize import NORMALIZER_VERSION, NormStats, normalize_block_text, normalize_pages
from ..processing.structure import join_cross_page_headings, structure_lines
from . import PIPELINE_VERSION, extractors
from .blobstore import BlobStore
from .model import BlockKind, ExtractedDocument, PageStatus
from .registry import Registry
from .validation import ValidationError, scan_sensitive_text, validate_file


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _lineage_versions() -> dict[str, Any]:
    v = extractors.versions()
    ocr = v.pop("ocr") or {}
    return {"pipeline": PIPELINE_VERSION, "normalizer": NORMALIZER_VERSION, "chunker": CHUNKER_VERSION,
            "strategy": STRATEGY, "libs": v, "ocr": f"{ocr.get('engine', 'none')} {ocr.get('version', '')}".strip()}


def idempotency_key(checksum: str, manifest: dict[str, Any]) -> str:
    blob = json.dumps({"c": checksum, "m": manifest, "v": _lineage_versions()}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Pipeline:
    def __init__(self, registry: Optional[Registry] = None, blobs: Optional[BlobStore] = None) -> None:
        self.registry = registry or Registry(config.REGISTRY_URL)
        self.blobs = blobs or BlobStore(config.BLOB_DIR)

    # ------------------------------------------------------------ submit

    def submit(self, file_name: str, data: bytes, manifest: Optional[dict[str, Any]] = None,
               principal: Optional[str] = None) -> tuple[dict[str, Any], bool]:
        """Validate and register an upload. Returns (job, created)."""
        manifest = manifest or {}
        checksum = sha256(data)
        key = idempotency_key(checksum, manifest)
        existing = self.registry.find_job_by_key(key)
        if existing:
            return existing, False
        try:
            v = validate_file(file_name, data)
        except ValidationError as exc:
            job = self.registry.create_job(key, checksum, file_name, None, manifest, principal)
            state = "quarantined" if exc.code in ("malware", "active_content", "zip_bomb", "path_traversal") \
                else "failed"
            job = self.registry.update_job(job["job_id"], state=state, stage="validate",
                                           error_code=exc.code, error_detail=exc.detail, retryable=False)
            return job, True
        self.blobs.put(checksum, data)
        job = self.registry.create_job(key, checksum, file_name, v.mime_type, manifest, principal)
        warnings = list(v.warnings) + ([f"malware scan {v.av}"] if v.av != "clean" else [])
        job = self.registry.update_job(job["job_id"], warnings=warnings, stage="extract")
        return job, True

    def submit_lms(self, item: dict[str, Any], principal: Optional[str] = None) -> tuple[dict[str, Any], bool]:
        """Register an LMS or EduNeX content item pushed as HTML or text."""
        body = item["body"]
        data = body.encode("utf-8")
        if len(data) > config.MAX_FILE_BYTES:
            raise ValidationError("too_large", "LMS body exceeds size limit")
        lms = item["lms"]
        manifest = {"metadata": dict(item.get("metadata") or {}), "lms_body_format": item.get("format", "html")}
        manifest["metadata"].setdefault("lms", lms)
        manifest["metadata"].setdefault("source_type", "lms_content")
        manifest["metadata"].setdefault("source_id", f"{lms.get('system', 'edunex')}:{lms['item_id']}".lower())
        manifest["metadata"].setdefault(
            "source_uri", f"edunex://course/{lms['course_id']}/item/{lms['item_id']}"
            + (f"?rev={lms['revision']}" if lms.get("revision") else ""))
        if item.get("title"):
            manifest["metadata"].setdefault("title", item["title"])
        checksum = sha256(data)
        key = idempotency_key(checksum, manifest)
        existing = self.registry.find_job_by_key(key)
        if existing:
            return existing, False
        self.blobs.put(checksum, data)
        mime = "text/html" if manifest["lms_body_format"] == "html" else "text/plain"
        file_name = f"{lms['item_id']}.{'html' if mime == 'text/html' else 'txt'}"
        job = self.registry.create_job(key, checksum, file_name, mime, manifest, principal)
        return self.registry.update_job(job["job_id"], stage="extract"), True

    # ------------------------------------------------------------ process

    def _fail(self, job_id: str, stage: str, code: str, detail: str, retryable: bool,
              state: str = "failed") -> dict[str, Any]:
        return self.registry.update_job(job_id, state=state, stage=stage, error_code=code,
                                        error_detail=detail[:1000], retryable=retryable)

    def process(self, job_id: str) -> dict[str, Any]:
        job = self.registry.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["state"] in ("succeeded", "partial", "duplicate", "quarantined") and job["stage"] in ("index", "done"):
            return job
        if job["state"] == "queued" and job["stage"] == "index":
            return job  # indexing retry, owned by the service
        if job["state"] == "quarantined" or (job["stage"] == "validate" and job["state"] == "failed"):
            return job
        attempts = job["attempts"] + 1
        if attempts > config.MAX_JOB_ATTEMPTS:
            return self._fail(job_id, job["stage"], "max_attempts", f"gave up after {job['attempts']} attempts",
                              retryable=False)
        self.registry.update_job(job_id, state="running", attempts=attempts, error_code=None, error_detail=None)
        warnings: list[str] = list(job["warnings"])

        data = self.blobs.get(job["checksum"])
        if data is None:
            return self._fail(job_id, "extract", "original_missing", "original file not found in blob store", False)

        # extract
        try:
            if job["mime_type"] in ("text/html", "text/plain"):
                doc = extractors.extract_lms_html(data.decode("utf-8"), job["mime_type"] == "text/html")
            else:
                doc = extractors.extract(job["mime_type"], data)
        except Exception as exc:
            return self._fail(job_id, "extract", "extraction_error", f"{type(exc).__name__}: {exc}", True)
        if doc.overall_status == "failed":
            return self._fail(job_id, "extract", "no_usable_text",
                              f"no page produced usable text: {json.dumps(doc.stats())}", True)

        # scan
        full_text = "\n".join(p.raw_text + "\n" + "\n".join(b.text for b in p.blocks) for p in doc.pages)
        blocking, scan_warn = scan_sensitive_text(full_text)
        warnings += scan_warn
        if blocking:
            return self._fail(job_id, "scan", "sensitive_content",
                              "document contains credentials or personal data: " + ", ".join(blocking),
                              retryable=False, state="quarantined")

        # metadata
        try:
            meta = self.build_metadata(job, doc, data)
        except PydanticValidationError as exc:
            errs = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
            return self._fail(job_id, "metadata", "metadata_invalid", errs, True)
        except ValueError as exc:
            return self._fail(job_id, "metadata", "metadata_invalid", str(exc), True)

        # dedupe by content across source ids
        same_bytes = self.registry.get_source_by_checksum(meta.content_checksum)
        if same_bytes and same_bytes["source_id"] != meta.source_id:
            return self.registry.update_job(
                job_id, state="duplicate", stage="done", version_id=same_bytes["version_id"],
                error_code="duplicate_content",
                error_detail=f"identical bytes already registered as {same_bytes['version_id']}",
                warnings=warnings, retryable=False)

        # chunk
        try:
            cfg_over = (job["manifest"] or {}).get("chunking") or {}
            cfg = ChunkingConfig(**{"subject_profile": profile_for_subject(meta.subject.value), **cfg_over})
            run, chunks = self.build_chunks(meta, doc, cfg)
        except Exception as exc:
            return self._fail(job_id, "chunk", "chunking_error", f"{type(exc).__name__}: {exc}", True)

        self.registry.upsert_source(meta.version_id, meta.source_id, meta.content_checksum,
                                    meta.model_dump(mode="json"))
        self.registry.save_run(run, chunks)
        if meta.approval_status == ApprovalStatus.APPROVED:
            superseded = self.registry.supersede_older(meta.source_id, meta.version_id)
            if superseded:
                warnings.append(f"superseded {', '.join(superseded)}")
        if run["quality"]["coverage"] < 0.98:
            warnings.append(f"content coverage {run['quality']['coverage']:.3f} below 0.98")
        stats = doc.stats()
        if stats["low_confidence_pages"]:
            warnings.append(f"low-confidence OCR pages: {stats['low_confidence_pages']}")
        if stats["failed_pages"]:
            warnings.append(f"failed pages: {stats['failed_pages']}")
        return self.registry.update_job(job_id, state="partial" if run["status"] == "partial" else "succeeded",
                                        stage="index", version_id=meta.version_id, run_id=run["run_id"],
                                        warnings=warnings, retryable=False)

    def retry(self, job_id: str, metadata_patch: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Re-run a job. Without a patch only a retryable failure is re-run. With
        a metadata patch (for example approval, or fixing a field that failed
        validation) any finished, non-quarantined job is re-processed against
        the stored original; the source version and its chunk ids stay stable."""
        job = self.registry.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["state"] in ("quarantined", "duplicate", "running", "queued"):
            return job
        if job["stage"] == "validate":
            return job  # the original was never accepted; upload a corrected file
        if not metadata_patch and not (job["state"] == "failed" and job["retryable"]):
            return job
        if metadata_patch:
            manifest = dict(job["manifest"])
            manifest["metadata"] = {**(manifest.get("metadata") or {}), **metadata_patch}
            self.registry.update_job(job_id, manifest=manifest)
        if not metadata_patch and job["stage"] == "index":
            # Chunks exist; only indexing failed. The service re-runs indexing.
            return self.registry.update_job(job_id, state="queued", error_code=None, error_detail=None)
        self.registry.update_job(job_id, state="queued", stage="extract",
                                 attempts=0 if metadata_patch else job["attempts"])
        return self.process(job_id)

    # ------------------------------------------------------------ metadata

    def build_metadata(self, job: dict[str, Any], doc: ExtractedDocument, data: bytes) -> SourceMetadata:
        fields: dict[str, Any] = {
            "approval_status": "pending_review",  # nothing is served until someone approves it
            "language": "en",
            "effective_from": dt.date.today().isoformat(),
            "source_uri": f"blob://sha256/{job['checksum'].split(':', 1)[1]}",
        }
        fields.update({k: v for k, v in draft_from_filename(job["file_name"]).items() if v is not None})
        if doc.doc_properties.get("title") and "title" not in fields:
            fields["title"] = doc.doc_properties["title"]
        manifest_meta = (job["manifest"] or {}).get("metadata") or {}
        for k, v in manifest_meta.items():
            if isinstance(v, dict) and isinstance(fields.get(k), dict):
                fields[k] = {**fields[k], **v}
            else:
                fields[k] = v
        fields.update({
            "content_checksum": job["checksum"],
            "file_name": job["file_name"],
            "mime_type": job["mime_type"],
            "file_size_bytes": len(data),
            "page_count": len(doc.pages) if doc.pages[0].basis != "none" else None,
            "ingested_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
        if "source_id" not in fields:
            raise ValueError("source_id could not be derived from the file name; supply it in the manifest")
        return SourceMetadata(**fields)

    # ------------------------------------------------------------ chunking

    def build_chunks(self, meta: SourceMetadata, doc: ExtractedDocument,
                     cfg: ChunkingConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        pblocks, norm = to_positioned_blocks(doc, cfg.subject_profile)
        id_seed = f"{meta.version_id}|{PIPELINE_VERSION}|{NORMALIZER_VERSION}|{doc.extractor_version}"
        grades = ",".join(str(g) for g in meta.grade_levels)
        prefix = f"{meta.subject.value} | Grade {grades}" + (f" | Term {meta.term}" if meta.term else "")
        prefix += f" | {meta.title}"
        chunks = chunk_blocks(pblocks, cfg, id_seed, context_prefix=prefix,
                              base_curriculum=meta.curriculum.model_dump(exclude_none=True, exclude={"outcome_codes"}))
        run_id = "run_" + uuid.uuid4().hex[:20]
        flat = meta.flat()
        records: list[dict[str, Any]] = []
        for c in chunks:
            loc = Locator(page_start=c.page_start, page_end=c.page_end, page_label=c.page_label,
                          page_basis=_basis(doc), section_path=c.section_path,
                          anchor=c.anchors[0] if c.anchors else None)
            records.append({
                "chunk_id": c.chunk_id, "ordinal": c.ordinal, "text": c.text, "embedding_text": c.embedding_text,
                "tokens": c.tokens, "prev_chunk_id": c.prev_chunk_id, "next_chunk_id": c.next_chunk_id,
                "section_id": c.section_id, "overlap_prefix_chars": c.overlap_prefix_chars,
                "block_kinds": c.block_kinds, "curriculum": c.curriculum,
                "locator": loc.model_dump(), "anchors": c.anchors,
                "citation": f"{meta.title}, {loc.cite()}".rstrip(", "),
                "source": flat,
                "extraction": {"run_id": run_id, "methods": c.methods, "min_ocr_confidence": c.min_ocr_confidence,
                               "low_confidence": c.min_ocr_confidence is not None
                               and c.min_ocr_confidence < config.OCR_LOW_CONFIDENCE,
                               "extractor": doc.extractor, "extractor_version": doc.extractor_version,
                               "ocr_engine": (doc.ocr or {}).get("version")},
                "chunking": {"strategy": STRATEGY, "chunker_version": CHUNKER_VERSION,
                             "normalizer_version": NORMALIZER_VERSION, "pipeline_version": PIPELINE_VERSION,
                             "config_hash": cfg.config_hash(), "config": cfg.model_dump()},
            })
        quality = quality_report(pblocks, chunks)
        quality["normalization"] = dict(norm.counts)
        run = {
            "run_id": run_id, "version_id": meta.version_id, "pipeline_version": PIPELINE_VERSION,
            "normalizer_version": NORMALIZER_VERSION, "chunker_version": CHUNKER_VERSION,
            "chunk_config_hash": cfg.config_hash(), "extractor": doc.extractor,
            "extractor_version": doc.extractor_version, "ocr": doc.ocr, "stats": doc.stats(),
            "quality": quality, "status": "partial" if doc.overall_status == "partial" else "succeeded",
        }
        return run, records


def _basis(doc: ExtractedDocument) -> str:
    return doc.pages[0].basis if doc.pages else "none"


def to_positioned_blocks(doc: ExtractedDocument, profile: str = "default") -> tuple[list[PositionedBlock], NormStats]:
    raw_idx = [i for i, p in enumerate(doc.pages) if p.raw_text.strip()]
    normalized, stats = normalize_pages([doc.pages[i].raw_text for i in raw_idx],
                                        [doc.pages[i].method == "ocr" for i in raw_idx])
    normalized = join_cross_page_headings(normalized)
    norm_by_page = dict(zip(raw_idx, normalized))
    aggressive = profile in ("mathematics", "physics", "chemistry")
    out: list[PositionedBlock] = []
    for i, p in enumerate(doc.pages):
        if p.status == PageStatus.FAILED:
            continue
        number = p.number if p.basis != "none" else None
        if i in norm_by_page:
            for b in structure_lines(norm_by_page[i], aggressive_equations=aggressive):
                if not b.anchor:
                    b.anchor = f"page{p.number}"
                out.append(PositionedBlock(b, number, p.label, p.ocr_confidence, p.method))
        for b in p.blocks:
            b.text = normalize_block_text(b.text, stats)
            if b.rows:
                b.rows = [[normalize_block_text(c, stats) for c in r] for r in b.rows]
            if b.text:
                out.append(PositionedBlock(b, number, p.label, p.ocr_confidence, p.method))
    return out, stats


_WORD = re.compile(r"\w+")


def quality_report(pblocks: list[PositionedBlock], chunks: list) -> dict[str, Any]:
    """Content preservation and shape metrics for one chunked document."""
    src = Counter(w.lower() for pb in pblocks for w in _WORD.findall(
        "\n".join(" | ".join(r) for r in pb.block.rows) if pb.block.rows else pb.block.text))
    got: Counter = Counter()
    for c in chunks:
        body = c.text[c.overlap_prefix_chars:] if c.overlap_prefix_chars else c.text
        got.update(w.lower() for w in _WORD.findall(" ".join(c.section_path) + "\n" + body))
    total = sum(src.values())
    covered = sum(min(n, got[w]) for w, n in src.items())
    toks = sorted(c.tokens for c in chunks) or [0]
    return {
        "coverage": round(covered / total, 4) if total else 1.0,
        "source_tokens": total,
        "chunks": len(chunks),
        "tokens_min": toks[0], "tokens_median": toks[len(toks) // 2], "tokens_max": toks[-1],
        "with_section_path": sum(1 for c in chunks if c.section_path),
        "with_page": sum(1 for c in chunks if c.page_start),
        "tables": sum(1 for pb in pblocks if pb.block.kind == BlockKind.TABLE),
    }
