"""Knowledge Service configuration (NEXA-ADR-0004).

Embeddings are obtained through the Model Gateway, never by importing an
embedding SDK here. The service presents its own service token to the gateway.
The vector store is pluggable; the M1 skeleton ships an in-memory backend and
defers the pgvector backend (NEXA-ADR-0002) to its own step.
"""
from __future__ import annotations
import os

# Model Gateway for embeddings.
GATEWAY_URL = os.environ.get("NEXA_GATEWAY_URL", "http://127.0.0.1:8100")

# Service token the Knowledge Service presents to the gateway. Minted from the
# identity CLI with role service. Required in any real deployment.
GATEWAY_TOKEN = os.environ.get("NEXA_KNOWLEDGE_GATEWAY_TOKEN", "")

# Logical embedding model name resolved by the gateway registry.
EMBED_MODEL = os.environ.get("NEXA_EMBED_MODEL", "nexa-embed-default")

# Request timeout to the gateway, seconds.
GATEWAY_TIMEOUT = float(os.environ.get("NEXA_GATEWAY_TIMEOUT", "120"))

# Which store backend to use: memory (tests and quick DEV) or pgvector
# (NEXA-ADR-0002, persistent, the default for QA, UAT and PROD).
STORE_BACKEND = os.environ.get("NEXA_KNOWLEDGE_STORE", "memory")

# PostgreSQL connection for the pgvector store, e.g.
# postgresql://nexa_knowledge@127.0.0.1:5432/nexa. Keep the password in
# /etc/nexa/knowledge.env or a .pgpass file, never in the repository.
PG_DSN = os.environ.get("NEXA_KNOWLEDGE_PG_DSN", "")
PG_SCHEMA = os.environ.get("NEXA_KNOWLEDGE_PG_SCHEMA", "knowledge")
PG_POOL_MAX = int(os.environ.get("NEXA_KNOWLEDGE_PG_POOL_MAX", "8"))

# ---- M2-004 embeddings and index versioning ----

# Task prefix scheme: auto (from the model version the gateway reports), none,
# or custom (NEXA_EMBED_DOC_PREFIX / NEXA_EMBED_QUERY_PREFIX).
EMBED_PREFIX_SCHEME = os.environ.get("NEXA_EMBED_PREFIX_SCHEME", "auto")
EMBED_DOC_PREFIX = os.environ.get("NEXA_EMBED_DOC_PREFIX", "")
EMBED_QUERY_PREFIX = os.environ.get("NEXA_EMBED_QUERY_PREFIX", "")
EMBED_BATCH = int(os.environ.get("NEXA_EMBED_BATCH", "32"))
EMBED_CACHE = os.environ.get("NEXA_EMBED_CACHE", "true").lower() == "true"

# A new index must pass validation before it can be activated: every expected
# chunk embedded, and self-retrieval recall@5 on a sample at or above this.
INDEX_SELF_RECALL_MIN = float(os.environ.get("NEXA_INDEX_SELF_RECALL_MIN", "0.8"))
INDEX_VALIDATION_SAMPLE = int(os.environ.get("NEXA_INDEX_VALIDATION_SAMPLE", "50"))

# pgvector HNSW parameters. ef_search is raised for filtered queries because
# pgvector 0.6 filters after the index scan.
HNSW_M = int(os.environ.get("NEXA_HNSW_M", "16"))
HNSW_EF_CONSTRUCTION = int(os.environ.get("NEXA_HNSW_EF_CONSTRUCTION", "64"))
HNSW_EF_SEARCH = int(os.environ.get("NEXA_HNSW_EF_SEARCH", "100"))

# Default number of results per query.
DEFAULT_TOP_K = int(os.environ.get("NEXA_KNOWLEDGE_TOP_K", "5"))

# ---- M2-005 retrieval ----

# PostgreSQL text search configuration for lexical retrieval. english stems
# (photosynthesis / photosynthetic) and drops stop words; the corpus is English.
FTS_CONFIG = os.environ.get("NEXA_KNOWLEDGE_FTS_CONFIG", "english")
# hybrid (vector + lexical, fused), vector, lexical, or auto: the default for
# the active index's embedding model, from the M2-005 benchmark on the DGX
# (RETRIEVAL_DEFAULTS below).
RETRIEVAL_MODE = os.environ.get("NEXA_RETRIEVAL_MODE", "auto")
# Reciprocal rank fusion constant and weights.
RRF_K = int(os.environ.get("NEXA_RRF_K", "60"))
RRF_WEIGHT_VECTOR = float(os.environ.get("NEXA_RRF_WEIGHT_VECTOR", "1.0"))
RRF_WEIGHT_LEXICAL = float(os.environ.get("NEXA_RRF_WEIGHT_LEXICAL", "1.0"))
# Reranking of the fused top candidates: on, off, or auto (per model, below).
RERANK = {"on": True, "off": False}.get(os.environ.get("NEXA_RERANK", "auto").lower())
# Per embedding model (prefix scheme), the configuration that scored best on
# the gold set in the M2-005 DGX benchmark, 2026-11-10; within 2 points of the
# best, the simpler and faster one. Score S (0.6 EN R@5 + 0.4 TPI R@5):
#   nomic-embed v1.5:   vector 79.0, hybrid 91.0, hybrid+rerank 87.0  -> hybrid
#   bge-m3 (CPU):       vector 96.0, hybrid 83.0, hybrid+rerank 95.0  -> vector
# English keyword matches outrank bge-m3's cross-lingual matches for Tok
# Pisin questions in hybrid fusion; nomic, which is English-only, gains from them.
RETRIEVAL_DEFAULTS = {
    "nomic-embed": {"mode": "hybrid", "rerank": False},
    "bge-m3": {"mode": "vector", "rerank": False},
    "default": {"mode": "hybrid", "rerank": False},
}
# Retrieval profile: central (DGX) or edge (school server). Sets how many
# candidates each retriever returns and how many are reranked.
RETRIEVAL_PROFILE = os.environ.get("NEXA_RETRIEVAL_PROFILE", "central")
PROFILES = {
    "central": {"candidates": 50, "rerank_pool": 30},
    "edge": {"candidates": 20, "rerank_pool": 15},
}

# ---- M2-006 grounding ----

# Evidence thresholds on the top passage's cosine similarity, per embedding
# prefix scheme (cosine scales differ by model): below min with low word
# coverage is no evidence; at or above strong is strong. Calibrated on the gold
# set by v1/tools/knowledge_retrieval_bench.py (NEXA-M2-006 section 4).
EVIDENCE_THRESHOLDS = {
    "nomic-embed": {"min": 0.59, "strong": 0.69},   # DGX calibration 2026-11-10, separation 0.882
    "bge-m3": {"min": 0.51, "strong": 0.59},        # DGX calibration 2026-11-10, separation 0.984
    "default": {"min": 0.50, "strong": 0.62},
}
if os.environ.get("NEXA_EVIDENCE_MIN") and os.environ.get("NEXA_EVIDENCE_STRONG"):
    EVIDENCE_THRESHOLDS = {"default": {"min": float(os.environ["NEXA_EVIDENCE_MIN"]),
                                       "strong": float(os.environ["NEXA_EVIDENCE_STRONG"])}}
# Share of the question's content words found in the top passage.
EVIDENCE_COVERAGE_STRONG = float(os.environ.get("NEXA_EVIDENCE_COVERAGE_STRONG", "0.6"))
EVIDENCE_COVERAGE_MIN = float(os.environ.get("NEXA_EVIDENCE_COVERAGE_MIN", "0.34"))
# A cited sentence sharing less than this share of its words with the cited
# passages is reported as weakly supported.
CITATION_MIN_SUPPORT = float(os.environ.get("NEXA_CITATION_MIN_SUPPORT", "0.25"))
# Answer generation through the Model Gateway.
ANSWER_MODEL = os.environ.get("NEXA_ANSWER_MODEL", "nexa-chat-precise")
ANSWER_TEMPERATURE = float(os.environ.get("NEXA_ANSWER_TEMPERATURE", "0.2"))
ANSWER_MAX_TOKENS = int(os.environ.get("NEXA_ANSWER_MAX_TOKENS", "700"))
ANSWER_TIMEOUT = float(os.environ.get("NEXA_ANSWER_TIMEOUT", "120"))
ANSWER_CONTEXT_CHARS = int(os.environ.get("NEXA_ANSWER_CONTEXT_CHARS", "7000"))

# Answer "what are the topics in Grade 11 Biology?" from the stored syllabus
# structure (outline.py) instead of a few searched passages.
CURRICULUM_OUTLINE = os.environ.get("NEXA_CURRICULUM_OUTLINE", "true").lower() == "true"

# General-knowledge answers when the curriculum has no evidence (general.py):
# off (strict refusal), llm (the general model) or web (web search, then the
# model over the results, falling back to llm). Always labelled as not from the
# approved curriculum, never as grounded.
GENERAL_ANSWERS = os.environ.get("NEXA_GENERAL_ANSWERS", "llm").lower()
if GENERAL_ANSWERS not in ("off", "llm", "web"):
    raise ValueError("NEXA_GENERAL_ANSWERS must be off, llm or web")
# Roles that may get general answers; "*" = everyone.
GENERAL_ROLES = frozenset(r.strip() for r in os.environ.get("NEXA_GENERAL_ROLES", "*").split(",") if r.strip())
# Model Gateway logical model for general answers; the answer model is the fallback.
GENERAL_MODEL = os.environ.get("NEXA_GENERAL_MODEL", "nexa-general")
GENERAL_TEMPERATURE = float(os.environ.get("NEXA_GENERAL_TEMPERATURE", "0.3"))
# Web search for mode "web": wikipedia (no key), tavily (key) or searxng (self-hosted URL).
WEB_SEARCH_PROVIDER = os.environ.get("NEXA_WEB_SEARCH_PROVIDER", "wikipedia").lower()
WEB_SEARCH_URL = os.environ.get("NEXA_WEB_SEARCH_URL", "")
WEB_SEARCH_API_KEY = os.environ.get("NEXA_WEB_SEARCH_API_KEY", "")
WEB_SEARCH_RESULTS = int(os.environ.get("NEXA_WEB_SEARCH_RESULTS", "4"))
WEB_SEARCH_TIMEOUT = float(os.environ.get("NEXA_WEB_SEARCH_TIMEOUT", "10"))

# ---- M2-007 LMS synchronisation ----

# Events per sync request, and how often failed retryable events are retried
# by the service itself (0 disables; the LMS also resends).
LMS_MAX_BATCH = int(os.environ.get("NEXA_LMS_MAX_BATCH", "100"))
LMS_RETRY_INTERVAL_S = int(os.environ.get("NEXA_LMS_RETRY_INTERVAL", "300"))
LMS_MAX_ATTEMPTS = int(os.environ.get("NEXA_LMS_MAX_ATTEMPTS", "5"))

# ---- M2 ingestion (NEXA-M2-002) ----

# Where the source registry, jobs and chunks are kept. sqlite:///path for DEV
# and single-host QA, memory for tests. PostgreSQL per ADR-0002 uses the DDL in
# v1/knowledge/migrations and lands with the pgvector retrieval step.
REGISTRY_URL = os.environ.get("NEXA_KNOWLEDGE_REGISTRY", "memory")

# Content-addressed store for original files, so a failed job can be retried
# without re-uploading. Empty means keep originals in memory (tests only).
BLOB_DIR = os.environ.get("NEXA_KNOWLEDGE_BLOB_DIR", "")

# Upload limits.
MAX_FILE_BYTES = int(os.environ.get("NEXA_KNOWLEDGE_MAX_FILE_MB", "60")) * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = int(os.environ.get("NEXA_KNOWLEDGE_MAX_UNZIPPED_MB", "400")) * 1024 * 1024
MAX_ZIP_ENTRIES = int(os.environ.get("NEXA_KNOWLEDGE_MAX_ZIP_ENTRIES", "5000"))
MAX_ZIP_RATIO = float(os.environ.get("NEXA_KNOWLEDGE_MAX_ZIP_RATIO", "200"))
MAX_PAGES = int(os.environ.get("NEXA_KNOWLEDGE_MAX_PAGES", "1500"))

# Malware scanning: none or clamav. REQUIRE_AV=true makes a missing or failing
# scanner reject the upload; set it in QA, UAT and PROD.
AV_SCANNER = os.environ.get("NEXA_KNOWLEDGE_AV", "none")
REQUIRE_AV = os.environ.get("NEXA_KNOWLEDGE_REQUIRE_AV", "false").lower() == "true"

# More distinct email addresses than this in one document blocks it as likely
# learner records rather than curriculum content.
MAX_EMAILS_BEFORE_BLOCK = int(os.environ.get("NEXA_KNOWLEDGE_MAX_EMAILS", "10"))

# OCR (tesseract). Pages with fewer native characters than OCR_MIN_CHARS are
# OCRed. Pages below OCR_LOW_CONFIDENCE mean word confidence are flagged.
OCR_ENABLED = os.environ.get("NEXA_KNOWLEDGE_OCR", "true").lower() == "true"
OCR_LANG = os.environ.get("NEXA_KNOWLEDGE_OCR_LANG", "eng")
OCR_DPI = int(os.environ.get("NEXA_KNOWLEDGE_OCR_DPI", "300"))
OCR_MIN_CHARS = int(os.environ.get("NEXA_KNOWLEDGE_OCR_MIN_CHARS", "60"))
OCR_LOW_CONFIDENCE = float(os.environ.get("NEXA_KNOWLEDGE_OCR_LOW_CONF", "70"))
OCR_TIMEOUT_S = int(os.environ.get("NEXA_KNOWLEDGE_OCR_TIMEOUT", "120"))

# Direct image uploads (PNG, JPEG, TIFF) hand untrusted bytes to Pillow's
# decoders. Pillow below 13.0.0 has open decoder advisories and Pillow 13 needs
# Python 3.11+, so image uploads are refused on older Pillow unless this is set
# to "force". PDF pages are rendered by pdfium, not decoded by Pillow, and are
# unaffected.
IMAGE_UPLOADS = os.environ.get("NEXA_KNOWLEDGE_IMAGE_UPLOADS", "auto")

# Re-embed every active, approved source into the active index at startup.
# Needed only with the in-memory store; pgvector keeps vectors across restarts.
REINDEX_ON_START = os.environ.get("NEXA_KNOWLEDGE_REINDEX_ON_START", "false").lower() == "true"

# Retries for a failed job before it is parked for an operator.
MAX_JOB_ATTEMPTS = int(os.environ.get("NEXA_KNOWLEDGE_MAX_ATTEMPTS", "3"))

# Bind address for this service.
BIND_HOST = os.environ.get("NEXA_KNOWLEDGE_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("NEXA_KNOWLEDGE_PORT", "8103"))
