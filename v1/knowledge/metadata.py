"""NEXA-M2-001 Curriculum Source and Metadata Standard, schema version 1.0.0.

This module is the single machine-readable contract for knowledge sources in
NEXA v1. Ingestion writes it, chunking copies it onto every chunk, retrieval
filters and weights on it, provenance renders citations from it, and evaluation
groups results by it. The human-readable standard is
docs/knowledge/NEXA-M2-001-curriculum-source-metadata-standard.md.

Versioning: SCHEMA_VERSION follows semver. A new optional field is a minor bump;
removing or changing the meaning of a field is a major bump and needs a
migration of stored records (NEXA-MIG-001).

Python 3.10 compatible (CI target).
"""
from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0.0"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
# BCP-47 primary tag plus optional region. PNG content is mainly en, tpi (Tok
# Pisin) and ho (Hiri Motu).
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
_URI_RE = re.compile(r"^(repo|edunex|lms|s3|blob|file|https)://\S+$")
# PNG STEM outcome codes: 12.1.1a, 11.3.2, 12.10.4b
OUTCOME_CODE_RE = re.compile(r"^\d{1,2}\.\d{1,2}(\.\d{1,2}[a-z]?)?$")


class SourceType(str, Enum):
    OFFICIAL_SYLLABUS = "official_syllabus"
    TEACHER_GUIDE = "teacher_guide"
    STUDENT_GUIDE = "student_guide"
    LESSON_NOTES = "lesson_notes"
    ASSESSMENT = "assessment"
    LMS_CONTENT = "lms_content"
    TEACHER_MATERIAL = "teacher_material"
    EXTERNAL_REFERENCE = "external_reference"


class AuthorityTier(int, Enum):
    """Lower number = more authoritative. See authority.py for conflict rules."""
    OFFICIAL_SYLLABUS = 1
    OFFICIAL_TEACHER_GUIDE = 2
    OFFICIAL_STUDENT_MATERIAL = 3
    APPROVED_LMS_CONTENT = 4
    TEACHER_CREATED = 5
    PERMITTED_EXTERNAL = 6


# The tier is derived from the source type, never chosen freely, so two
# ingestion paths can not disagree about how authoritative a document is.
SOURCE_TYPE_TIER: dict[SourceType, AuthorityTier] = {
    SourceType.OFFICIAL_SYLLABUS: AuthorityTier.OFFICIAL_SYLLABUS,
    SourceType.TEACHER_GUIDE: AuthorityTier.OFFICIAL_TEACHER_GUIDE,
    SourceType.STUDENT_GUIDE: AuthorityTier.OFFICIAL_STUDENT_MATERIAL,
    SourceType.LESSON_NOTES: AuthorityTier.OFFICIAL_STUDENT_MATERIAL,
    SourceType.ASSESSMENT: AuthorityTier.OFFICIAL_TEACHER_GUIDE,
    SourceType.LMS_CONTENT: AuthorityTier.APPROVED_LMS_CONTENT,
    SourceType.TEACHER_MATERIAL: AuthorityTier.TEACHER_CREATED,
    SourceType.EXTERNAL_REFERENCE: AuthorityTier.PERMITTED_EXTERNAL,
}

OFFICIAL_TYPES = {
    SourceType.OFFICIAL_SYLLABUS,
    SourceType.TEACHER_GUIDE,
    SourceType.STUDENT_GUIDE,
    SourceType.LESSON_NOTES,
    SourceType.ASSESSMENT,
}


class Scope(str, Enum):
    NATIONAL = "national"
    PROVINCIAL = "provincial"
    SCHOOL = "school"
    CLASS = "class"


class ApprovalStatus(str, Enum):
    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    DRAFT = "draft"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


# Only these are served to learners and agents. Pending and draft material can
# be ingested for review but is filtered out of retrieval.
RETRIEVABLE_STATUSES = {ApprovalStatus.APPROVED}


class AccessClassification(str, Enum):
    PUBLIC = "public"              # anyone, including unauthenticated previews
    LEARNER = "learner"            # any authenticated student or staff member
    TEACHER_ONLY = "teacher_only"  # staff roles only (answer keys, teacher guides)
    RESTRICTED = "restricted"      # named roles only (assessments before release)


class Subject(str, Enum):
    BIOLOGY = "Biology"
    CHEMISTRY = "Chemistry"
    PHYSICS = "Physics"
    MATHEMATICS = "Mathematics"
    ENGLISH = "English"
    ENGINEERING = "Engineering"
    TECHNOLOGY = "Technology"
    GENERAL_SCIENCE = "General Science"
    SOCIAL_SCIENCE = "Social Science"
    GENERAL = "General"


class CurriculumPosition(BaseModel):
    """Where a source sits in the curriculum. All optional at source level;
    chunks inherit and refine this (a chunk knows its own topic and outcomes)."""
    model_config = ConfigDict(extra="forbid")

    thema: Optional[str] = Field(None, description="PNG STEM thematic area, e.g. Biotechnology")
    strand: Optional[str] = Field(None, description="Strand for non-STEM syllabi")
    module_code: Optional[str] = Field(None, description="e.g. 12.1")
    module_title: Optional[str] = None
    topic_code: Optional[str] = Field(None, description="e.g. 12.1.1")
    topic_title: Optional[str] = None
    outcome_codes: list[str] = Field(default_factory=list, description="e.g. 12.1.1a")

    @field_validator("outcome_codes")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        bad = [c for c in v if not OUTCOME_CODE_RE.match(c)]
        if bad:
            raise ValueError(f"invalid outcome codes: {bad}")
        return v


class Jurisdiction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field("PG", pattern=r"^[A-Z]{2}$")
    province: Optional[str] = None
    school_id: Optional[str] = None
    class_id: Optional[str] = None


class LmsReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: Literal["edunex", "lms"] = "edunex"
    course_id: str
    item_id: str
    revision: Optional[str] = None


class ExternalReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(..., description="Must be on the permitted external allowlist")
    retrieved_at: dt.datetime
    license: Optional[str] = None


# Permitted external domains (NEXA-GOV-001). Extend by governance decision only.
PERMITTED_EXTERNAL_DOMAINS = {
    "en.wikipedia.org",
    "scholar.google.com",
    "openstax.org",
    "phet.colorado.edu",
    "www.khanacademy.org",
}


class SourceMetadata(BaseModel):
    """Metadata for one version of one knowledge source document."""
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION

    # Identity and versioning
    source_id: str = Field(..., description="Stable across versions, e.g. png-ndoe:stem-chemistry-syllabus-gr12-t1")
    content_checksum: str = Field(..., description="sha256:<hex> of the original file bytes")
    version_label: Optional[str] = Field(None, description="Edition or version as printed, e.g. 2023 or Edition 2")
    supersedes_checksum: Optional[str] = None

    # Classification
    source_type: SourceType
    authority_tier: Optional[AuthorityTier] = Field(None, description="Derived from source_type")
    title: str = Field(..., min_length=3, max_length=300)
    subject: Subject
    grade_levels: list[int] = Field(..., min_length=1)
    term: Optional[int] = Field(None, ge=1, le=4)
    curriculum: CurriculumPosition = Field(default_factory=CurriculumPosition)
    language: str = "en"

    # Authority and governance
    scope: Scope
    jurisdiction: Jurisdiction = Field(default_factory=Jurisdiction)
    publisher: Optional[str] = None
    approval_status: ApprovalStatus
    approved_by: Optional[str] = None
    access_classification: AccessClassification

    # Dates
    effective_from: dt.date
    effective_to: Optional[dt.date] = None
    ingested_at: Optional[dt.datetime] = Field(None, description="Set by the ingestion pipeline")

    # Location of the original
    source_uri: str
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size_bytes: Optional[int] = Field(None, ge=0)
    page_count: Optional[int] = Field(None, ge=0)

    # Origin-specific references
    lms: Optional[LmsReference] = None
    external: Optional[ExternalReference] = None

    tags: list[str] = Field(default_factory=list)

    # ---- field rules ----

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, v: str) -> str:
        if not _SOURCE_ID_RE.match(v):
            raise ValueError("source_id must be lowercase [a-z0-9._:-], 3 to 128 chars")
        return v

    @field_validator("content_checksum", "supersedes_checksum")
    @classmethod
    def _checksum(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SHA256_RE.match(v):
            raise ValueError("checksum must be sha256:<64 lowercase hex>")
        return v

    @field_validator("grade_levels")
    @classmethod
    def _grades(cls, v: list[int]) -> list[int]:
        if any(g < 1 or g > 13 for g in v):
            raise ValueError("grade levels must be 1 to 13")
        return sorted(set(v))

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        if not _LANG_RE.match(v):
            raise ValueError("language must be a BCP-47 tag such as en, tpi, ho")
        return v

    @field_validator("source_uri")
    @classmethod
    def _uri(cls, v: str) -> str:
        if not _URI_RE.match(v):
            raise ValueError("source_uri must use repo://, edunex://, lms://, s3://, blob://, file:// or https://")
        if re.search(r"(?i)(token|secret|password|api[_-]?key|sig)=", v):
            raise ValueError("source_uri must not carry credentials or signed query parameters")
        return v

    # ---- cross-field rules ----

    @model_validator(mode="after")
    def _rules(self) -> "SourceMetadata":
        derived = SOURCE_TYPE_TIER[self.source_type]
        if self.authority_tier is None:
            self.authority_tier = derived
        elif self.authority_tier != derived:
            raise ValueError(
                f"authority_tier {self.authority_tier.value} does not match source_type "
                f"{self.source_type.value} (expected {derived.value})"
            )

        if self.source_type in OFFICIAL_TYPES:
            if self.scope != Scope.NATIONAL:
                raise ValueError("official curriculum sources must have national scope")
            if not self.publisher:
                raise ValueError("official curriculum sources require a publisher")

        if self.scope in (Scope.SCHOOL, Scope.CLASS) and not self.jurisdiction.school_id:
            raise ValueError("school and class scope require jurisdiction.school_id")
        if self.scope == Scope.CLASS and not self.jurisdiction.class_id:
            raise ValueError("class scope requires jurisdiction.class_id")
        if self.scope == Scope.PROVINCIAL and not self.jurisdiction.province:
            raise ValueError("provincial scope requires jurisdiction.province")

        if self.source_type == SourceType.LMS_CONTENT and self.lms is None:
            raise ValueError("lms_content requires an lms reference")
        if self.source_type == SourceType.EXTERNAL_REFERENCE:
            if self.external is None:
                raise ValueError("external_reference requires external.domain and retrieved_at")
            if self.external.domain not in PERMITTED_EXTERNAL_DOMAINS:
                raise ValueError(f"external domain {self.external.domain} is not permitted")

        if self.approval_status == ApprovalStatus.APPROVED and self.source_type in OFFICIAL_TYPES:
            if not self.approved_by:
                raise ValueError("approved official sources require approved_by")

        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to is before effective_from")
        return self

    # ---- helpers ----

    @property
    def version_id(self) -> str:
        """Stable id of this exact version: source_id@<first 16 hex of checksum>."""
        return f"{self.source_id}@{self.content_checksum.split(':', 1)[1][:16]}"

    def is_retrievable(self, on: Optional[dt.date] = None) -> bool:
        on = on or dt.date.today()
        if self.approval_status not in RETRIEVABLE_STATUSES:
            return False
        if on < self.effective_from:
            return False
        if self.effective_to and on > self.effective_to:
            return False
        return True

    def flat(self) -> dict[str, Any]:
        """Flat, scalar-only view for vector store metadata and filters."""
        c = self.curriculum
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "version_id": self.version_id,
            "content_checksum": self.content_checksum,
            "source_type": self.source_type.value,
            "authority_tier": int(self.authority_tier.value),
            "title": self.title,
            "subject": self.subject.value,
            "grade_levels": ",".join(str(g) for g in self.grade_levels),
            "term": self.term,
            "thema": c.thema,
            "module_code": c.module_code,
            "language": self.language,
            "scope": self.scope.value,
            "school_id": self.jurisdiction.school_id,
            "approval_status": self.approval_status.value,
            "access_classification": self.access_classification.value,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "source_uri": self.source_uri,
        }


class Locator(BaseModel):
    """Where inside a source a piece of text came from. Carried by every page,
    block and chunk so a citation can point at the original."""
    model_config = ConfigDict(extra="forbid")

    page_start: Optional[int] = Field(None, ge=1, description="1-based physical page or slide")
    page_end: Optional[int] = Field(None, ge=1)
    page_label: Optional[str] = Field(None, description="Printed page label, e.g. VIII or 12")
    page_basis: Literal["physical", "rendered_estimate", "slide", "none"] = "physical"
    section_path: list[str] = Field(default_factory=list)
    anchor: Optional[str] = Field(None, description="Stable in-document anchor, e.g. docx paragraph index")

    def cite(self) -> str:
        parts: list[str] = []
        if self.page_start:
            unit = "slide" if self.page_basis == "slide" else "p."
            if self.page_end and self.page_end != self.page_start:
                parts.append(f"{unit} {self.page_start}-{self.page_end}")
            else:
                parts.append(f"{unit} {self.page_start}")
            if self.page_basis == "rendered_estimate":
                parts[-1] += " (approx.)"
            if self.page_label and self.page_label != str(self.page_start):
                parts[-1] += f" [printed {self.page_label}]"
        if self.section_path:
            parts.append(" > ".join(self.section_path))
        return ", ".join(parts)


def json_schema() -> dict[str, Any]:
    """JSON Schema of the source contract, published for non-Python consumers."""
    schema = SourceMetadata.model_json_schema()
    schema["$id"] = f"https://nexa.learnx.ai/schemas/source-metadata/{SCHEMA_VERSION}.json"
    schema["title"] = f"NEXA Source Metadata {SCHEMA_VERSION}"
    return schema
