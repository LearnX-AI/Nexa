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


class QueryResponse(BaseModel):
    query: str
    results: list[QueryResult]
    x_nexa: Optional[dict[str, Any]] = None
