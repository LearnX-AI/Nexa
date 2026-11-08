"""Request and response models for the Knowledge Service."""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel


class Document(BaseModel):
    text: str
    metadata: dict[str, Any] = {}


class IngestRequest(BaseModel):
    documents: list[Document]


class IngestResponse(BaseModel):
    ingested: int
    x_nexa: Optional[dict[str, Any]] = None


class QueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = None


class QueryResult(BaseModel):
    text: str
    score: float
    metadata: dict[str, Any] = {}
    citation: Optional[str] = None


class QueryResponse(BaseModel):
    query: str
    results: list[QueryResult]
    x_nexa: Optional[dict[str, Any]] = None


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
