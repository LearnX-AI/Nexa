"""Request and response models for the Knowledge Service."""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class Document(BaseModel):
    text: str
    metadata: dict[str, Any] = {}


class IngestRequest(BaseModel):
    documents: list[Document]


class IngestResponse(BaseModel):
    ingested: int
    x_nexa: Optional[dict[str, Any]] = None


class QueryFilters(BaseModel):
    """Narrow retrieval (NEXA-M2-005). Filters only ever narrow what the
    caller's access allows."""
    model_config = {"extra": "forbid"}

    subjects: Optional[list[str]] = None
    grades: Optional[list[int]] = None
    languages: Optional[list[str]] = None
    source_types: Optional[list[str]] = None
    module_codes: Optional[list[str]] = None
    topic_codes: Optional[list[str]] = None
    scopes: Optional[list[Literal["national", "provincial", "school", "class"]]] = None
    max_tier: Optional[int] = Field(None, ge=1, le=6)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: Optional[int] = Field(None, ge=1, le=50)
    filters: Optional[QueryFilters] = None
    mode: Optional[Literal["hybrid", "vector", "lexical"]] = None
    rerank: Optional[bool] = None
    profile: Optional[Literal["central", "edge"]] = None
    explain: bool = False


class QueryResult(BaseModel):
    text: str
    score: float = Field(..., description="Semantic similarity (cosine) of the passage to the query")
    metadata: dict[str, Any] = {}
    citation: Optional[str] = None
    chunk_id: Optional[str] = None
    rank: Optional[int] = None
    scores: Optional[dict[str, Any]] = None
    reasons: Optional[list[str]] = None
    authority: Optional[dict[str, Any]] = None
    provenance: Optional[dict[str, Any]] = None


class QueryResponse(BaseModel):
    query: str
    results: list[QueryResult]
    retrieval: Optional[dict[str, Any]] = None
    x_nexa: Optional[dict[str, Any]] = None


class AnswerRequest(BaseModel):
    """A grounded answer (NEXA-M2-006)."""
    model_config = {"extra": "forbid"}

    question: str = Field(..., min_length=1, max_length=2000)
    top_k: Optional[int] = Field(None, ge=1, le=12)
    filters: Optional[QueryFilters] = None
    audience: Optional[Literal["student", "teacher"]] = None
    explain: bool = False


# ---- M2 ingestion API ----

class IngestManifest(BaseModel):
    """Governance and override fields supplied with an upload. metadata keys
    are SourceMetadata fields and win over anything inferred from the file."""
    model_config = {"extra": "forbid"}

    metadata: dict[str, Any] = {}
    chunking: Optional[dict[str, Any]] = None


class LmsReferenceIn(BaseModel):
    system: str = "edunex"
    course_id: str
    item_id: str
    revision: Optional[str] = None


class LmsItemRequest(BaseModel):
    lms: LmsReferenceIn
    title: Optional[str] = None
    body: str
    format: str = "html"
    metadata: dict[str, Any] = {}


class JobRetryRequest(BaseModel):
    metadata_patch: Optional[dict[str, Any]] = None


# ---- M2-004 index versions ----

class IndexBuildRequest(BaseModel):
    """Build a new index beside the active one. embed_model is a logical model
    name the Model Gateway serves (default: NEXA_EMBED_MODEL)."""
    model_config = {"extra": "forbid"}

    embed_model: Optional[str] = None
    activate_when_ready: bool = False
    note: str = ""


class IndexActivateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    force: bool = False
