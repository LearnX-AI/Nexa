"""pgvector store (NEXA-ADR-0002, NEXA-M2-004).

Layout in schema NEXA_KNOWLEDGE_PG_SCHEMA (default knowledge):
- index_versions   one row per index version (record as jsonb, status)
- index_alias      the single 'active' pointer, switched in one transaction
- embedding_cache  (fingerprint, text hash) -> vector, so unchanged text is
                   never re-embedded by a rebuild
- vec_<index_id>   one table per index version: a fixed-dimension vector column
                   with an HNSW cosine index (pgvector needs a fixed dimension
                   to index), the chunk text and metadata, and typed filter
                   columns for access control. Retiring an index drops it.

The pgvector extension must already exist in the database (CREATE EXTENSION
needs elevated rights; see migrations/0002_vector_index.sql). The service role
needs CREATE on the schema to add per-index tables.

pgvector 0.6 applies WHERE filters after the HNSW scan, so filtered queries
raise hnsw.ef_search to keep enough candidates.

NEXA-M2-005 adds, to every index table, typed filter columns (subject, grades,
language, source type, authority tier, curriculum codes, school) and a
full-text column (tsv, GIN-indexed) for lexical retrieval. All of them are
generated from the stored metadata and text, so writes are unchanged and the
columns can never disagree with the metadata. Tables created before M2-005
get the columns added in place when the store opens.
"""
from __future__ import annotations

import json
import logging
import re
import struct
from typing import Any, Optional

from . import config
from .store import IndexRecord, StoreError, VectorStore

_ID_RE = re.compile(r"^[a-z0-9_]{3,48}$")
_RECORD_FIELDS = set(IndexRecord.__dataclass_fields__)
log = logging.getLogger("nexa.knowledge.pgstore")


def _generated_columns(fts: str) -> list[tuple[str, str]]:
    """(column, definition) for the M2-005 filter and full-text columns. The
    expressions are immutable, as generated columns require."""
    if not re.fullmatch(r"[a-z_]{2,32}", fts):
        raise StoreError("invalid full-text configuration name")
    def txt(k):
        return f"text GENERATED ALWAYS AS (metadata->>'{k}') STORED"
    return [
        ("subject", txt("subject")),
        ("language", txt("language")),
        ("source_type", txt("source_type")),
        ("module_code", txt("module_code")),
        ("topic_code", txt("topic_code")),
        ("school_id", txt("school_id")),
        ("authority_tier", "int GENERATED ALWAYS AS (CASE WHEN metadata->>'authority_tier' ~ '^[0-9]{1,2}$' "
                           "THEN (metadata->>'authority_tier')::int END) STORED"),
        ("grade_levels", "int[] GENERATED ALWAYS AS (CASE WHEN metadata->>'grade_levels' ~ "
                         "'^[0-9]{1,2}(,[0-9]{1,2})*$' THEN string_to_array(metadata->>'grade_levels', ',')::int[] "
                         "END) STORED"),
        # Body text weighted A, title and section path B: a heading match counts,
        # but less than the passage itself.
        ("tsv", f"tsvector GENERATED ALWAYS AS ("
                f"setweight(to_tsvector('{fts}'::regconfig, coalesce(metadata->>'title', '') || ' ' || "
                f"coalesce(metadata->>'section_path', '')), 'B') || "
                f"setweight(to_tsvector('{fts}'::regconfig, text), 'A')) STORED"),
    ]


_TERM_RE = re.compile(r"[a-z0-9]+")


def tsquery_text(query: str) -> str:
    """An OR query over the question's content words. A question is not a
    conjunction: requiring every word (websearch_to_tsquery) misses passages
    that answer it in other words. Ranking does the rest."""
    from .benchmark.bm25 import tokenize
    terms = list(dict.fromkeys(t for t in tokenize(query) if _TERM_RE.fullmatch(t)))[:32]
    return " | ".join(terms)


def _literal(text: str) -> str:
    """SQL string literal for DDL that takes no parameters (COMMENT ON)."""
    return "'" + text.replace("'", "''") + "'"


def _vec(v: list[float]) -> str:
    # 9 significant digits round-trip any float32 exactly (8 do not).
    return "[" + ",".join(f"{x:.9g}" for x in v) + "]"


def _f32(v) -> list[float]:
    """Exact float32 values as Python floats. PostgreSQL prints a real in its
    shortest form, which Python reads back as a nearby but different double;
    re-quantising makes every read equal, bit for bit, to what was written."""
    v = [float(x) for x in v]
    return list(struct.unpack(f"{len(v)}f", struct.pack(f"{len(v)}f", *v)))


def _parse_vec(s: Any) -> list[float]:
    if isinstance(s, list):
        return _f32(s)
    return _f32(x for x in str(s).strip("[]").split(",") if x)


class PgVectorStore(VectorStore):
    name = "pgvector"

    def __init__(self, dsn: str, schema: str = "knowledge") -> None:
        if not dsn:
            raise StoreError("NEXA_KNOWLEDGE_PG_DSN is required for the pgvector store")
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
            raise StoreError("invalid schema name")
        self.dsn = dsn
        self.schema = schema
        self._pool = None
        self.fts_config = config.FTS_CONFIG
        self._no_lexical: set[str] = set()   # index tables that could not be migrated

    # ---------------------------------------------------------------- plumbing

    async def open(self) -> None:
        if self._pool is not None:
            return
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=config.PG_POOL_MAX, open=False,
                                         kwargs={"autocommit": True})
        await self._pool.open(wait=True, timeout=30)
        async with self._pool.connection() as conn:
            ext = await (await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).fetchone()
            if not ext:
                raise StoreError("the pgvector extension is not installed in this database "
                                 "(run migrations/0002_vector_index.sql as an administrator)")
            s = self.schema
            await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.index_versions (
                    index_id   text PRIMARY KEY,
                    status     text NOT NULL,
                    record     jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now())""")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.index_alias (
                    name     text PRIMARY KEY,
                    index_id text NOT NULL REFERENCES {s}.index_versions (index_id))""")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {s}.embedding_cache (
                    fingerprint text NOT NULL,
                    text_key    text NOT NULL,
                    embedding   real[] NOT NULL,
                    created_at  timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (fingerprint, text_key))""")
        self.pgvector_version = ext[0]
        for rec in await self.list_indexes():
            if rec.status != "retired":
                await self._ensure_columns(rec.index_id)

    async def _ensure_columns(self, index_id: str) -> None:
        """Add the M2-005 columns to an index table created before them. Adding
        a stored generated column rewrites the table once; on failure (for
        example a table owned by another role) the index keeps working for
        vector queries and reports lexical retrieval as unavailable."""
        t = self._table(index_id)
        try:
            async with self._conn() as conn:
                have = {r[0] for r in await (await conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                    "AND table_name = %s", (self.schema, f"vec_{index_id}"))).fetchall()}
                if not have:
                    return
                for col, ddl in _generated_columns(self.fts_config):
                    if col not in have:
                        await conn.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} {ddl}")
                await conn.execute(f"CREATE INDEX IF NOT EXISTS vec_{index_id}_tsv ON {t} USING gin (tsv)")
            self._no_lexical.discard(index_id)
        except Exception as exc:  # noqa: BLE001 - degrade, do not fail startup
            log.warning("index %s: lexical columns unavailable: %s", index_id, exc)
            self._no_lexical.add(index_id)

    def lexical_available(self, index_id: str) -> bool:
        return index_id not in self._no_lexical

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _conn(self):
        if self._pool is None:
            raise StoreError("store is not open")
        return self._pool.connection()

    def _table(self, index_id: str) -> str:
        if not _ID_RE.match(index_id):
            raise StoreError(f"invalid index id {index_id!r}")
        return f"{self.schema}.vec_{index_id}"

    @staticmethod
    def _rec(row) -> IndexRecord:
        data = row if isinstance(row, dict) else json.loads(row)
        return IndexRecord(**{k: v for k, v in data.items() if k in _RECORD_FIELDS})

    # ---------------------------------------------------------------- indexes

    async def create_index(self, rec: IndexRecord) -> IndexRecord:
        t = self._table(rec.index_id)
        if rec.dim < 1 or rec.dim > 16000:
            raise StoreError(f"unsupported dimension {rec.dim}")
        async with self._conn() as conn, conn.transaction():
            await conn.execute(f"INSERT INTO {self.schema}.index_versions (index_id, status, record) "
                               f"VALUES (%s, %s, %s)", (rec.index_id, rec.status, json.dumps(rec.to_dict())))
            await conn.execute(f"""
                CREATE TABLE {t} (
                    chunk_id              text PRIMARY KEY,
                    version_id            text NOT NULL,
                    text                  text NOT NULL,
                    metadata              jsonb NOT NULL,
                    approval_status       text,
                    access_classification text,
                    scope                 text,
                    effective_from        date,
                    effective_to          date,
                    embedding             vector({int(rec.dim)}) NOT NULL,
                    """ + ",\n".join(f"{c} {d}" for c, d in _generated_columns(self.fts_config)) + ")")
            await conn.execute(f"CREATE INDEX ON {t} (version_id)")
            await conn.execute(f"CREATE INDEX vec_{rec.index_id}_tsv ON {t} USING gin (tsv)")
            # The collection itself carries its version, for anyone reading the database directly.
            await conn.execute(f"COMMENT ON TABLE {t} IS " + _literal(
                f"NEXA index {rec.index_id}; embedding {rec.fingerprint}; tokenizer {rec.tokenizer}; "
                f"normalization {rec.normalization}; distance {rec.distance}"))
            if rec.dim <= 2000:  # pgvector HNSW limit for vector
                await conn.execute(
                    f"CREATE INDEX ON {t} USING hnsw (embedding vector_cosine_ops) "
                    f"WITH (m = {int(config.HNSW_M)}, ef_construction = {int(config.HNSW_EF_CONSTRUCTION)})")
        return rec

    async def list_indexes(self) -> list[IndexRecord]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT record FROM {self.schema}.index_versions ORDER BY created_at, index_id")).fetchall()
        return [self._rec(r[0]) for r in rows]

    async def get_index(self, index_id: str) -> Optional[IndexRecord]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT record FROM {self.schema}.index_versions WHERE index_id = %s", (index_id,))).fetchone()
        return self._rec(row[0]) if row else None

    async def update_index(self, index_id: str, **fields: Any) -> IndexRecord:
        rec = await self.get_index(index_id)
        if rec is None:
            raise StoreError(f"unknown index {index_id}")
        for k, v in fields.items():
            setattr(rec, k, v)
        async with self._conn() as conn:
            await conn.execute(f"UPDATE {self.schema}.index_versions SET status = %s, record = %s "
                               f"WHERE index_id = %s", (rec.status, json.dumps(rec.to_dict()), index_id))
        return rec

    async def active_index_id(self) -> Optional[str]:
        async with self._conn() as conn:
            row = await (await conn.execute(
                f"SELECT index_id FROM {self.schema}.index_alias WHERE name = 'active'")).fetchone()
        return row[0] if row else None

    async def set_active(self, index_id: str) -> IndexRecord:
        s = self.schema
        async with self._conn() as conn, conn.transaction():
            # Serialise switches; the alias row is the lock.
            await conn.execute(f"LOCK TABLE {s}.index_alias IN EXCLUSIVE MODE")
            row = await (await conn.execute(f"SELECT index_id FROM {s}.index_alias WHERE name = 'active'")).fetchone()
            prev = row[0] if row else None
            rec_row = await (await conn.execute(f"SELECT record FROM {s}.index_versions WHERE index_id = %s",
                                                (index_id,))).fetchone()
            if not rec_row:
                raise StoreError(f"unknown index {index_id}")
            rec = self._rec(rec_row[0])
            if prev and prev != index_id:
                prow = await (await conn.execute(f"SELECT record FROM {s}.index_versions WHERE index_id = %s",
                                                 (prev,))).fetchone()
                if prow:
                    p = self._rec(prow[0])
                    p.status = "ready"
                    await conn.execute(f"UPDATE {s}.index_versions SET status = 'ready', record = %s "
                                       f"WHERE index_id = %s", (json.dumps(p.to_dict()), prev))
                rec.previous_index_id = prev
            from .store import _now
            rec.status, rec.activated_at = "active", _now()
            await conn.execute(f"UPDATE {s}.index_versions SET status = 'active', record = %s WHERE index_id = %s",
                               (json.dumps(rec.to_dict()), index_id))
            await conn.execute(f"INSERT INTO {s}.index_alias (name, index_id) VALUES ('active', %s) "
                               f"ON CONFLICT (name) DO UPDATE SET index_id = EXCLUDED.index_id", (index_id,))
        return rec

    async def drop_index(self, index_id: str) -> None:
        if index_id == await self.active_index_id():
            raise StoreError("cannot drop the active index")
        t = self._table(index_id)
        async with self._conn() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {t}")
        if await self.get_index(index_id):
            await self.update_index(index_id, status="retired")

    # ---------------------------------------------------------------- vectors

    async def upsert(self, index_id: str, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        rec = await self.get_index(index_id)
        if rec is None:
            raise StoreError(f"unknown index {index_id}")
        rows = []
        for it in items:
            if len(it["vector"]) != rec.dim:
                raise StoreError(f"vector dimension {len(it['vector'])} does not match index dimension {rec.dim}")
            md = it.get("metadata") or {}
            rows.append((it["chunk_id"], it["version_id"], it["text"], json.dumps(md, default=str),
                         md.get("approval_status"), md.get("access_classification"), md.get("scope"),
                         md.get("effective_from"), md.get("effective_to"), _vec(it["vector"])))
        t = self._table(index_id)
        async with self._conn() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(
                f"INSERT INTO {t} (chunk_id, version_id, text, metadata, approval_status, access_classification, "
                f"scope, effective_from, effective_to, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) "
                f"ON CONFLICT (chunk_id) DO UPDATE SET version_id = EXCLUDED.version_id, text = EXCLUDED.text, "
                f"metadata = EXCLUDED.metadata, approval_status = EXCLUDED.approval_status, "
                f"access_classification = EXCLUDED.access_classification, scope = EXCLUDED.scope, "
                f"effective_from = EXCLUDED.effective_from, effective_to = EXCLUDED.effective_to, "
                f"embedding = EXCLUDED.embedding "
                # Leave identical rows untouched. Every rewritten row is a new
                # entry in the HNSW graph, which shifts approximate results; a
                # re-index or rollback catch-up of unchanged content must not.
                f"WHERE ({t}.version_id, {t}.text, {t}.metadata, {t}.embedding) IS DISTINCT FROM "
                f"(EXCLUDED.version_id, EXCLUDED.text, EXCLUDED.metadata, EXCLUDED.embedding)", rows)
        return len(rows)

    async def _live_tables(self, index_id: Optional[str]) -> list[str]:
        ids = [index_id] if index_id else [r.index_id for r in await self.list_indexes() if r.status != "retired"]
        return [self._table(i) for i in ids]

    async def delete_version(self, version_id: str, index_id: Optional[str] = None) -> int:
        n = 0
        async with self._conn() as conn:
            for t in await self._live_tables(index_id):
                cur = await conn.execute(f"DELETE FROM {t} WHERE version_id = %s", (version_id,))
                n += cur.rowcount or 0
        return n

    async def delete_chunks(self, index_id: str, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        async with self._conn() as conn:
            cur = await conn.execute(f"DELETE FROM {self._table(index_id)} WHERE chunk_id = ANY(%s)", (chunk_ids,))
            return cur.rowcount or 0

    async def query(self, index_id: str, vector: list[float], top_k: int, where=None) -> list[dict[str, Any]]:
        t = self._table(index_id)
        clause, params = "TRUE", []
        if where is not None:
            if not hasattr(where, "sql"):
                raise StoreError("the pgvector store needs a RetrievalFilter, not an arbitrary callable")
            clause, params = where.sql()
        v = _vec(vector)
        async with self._conn() as conn, conn.transaction():
            ef = max(config.HNSW_EF_SEARCH, top_k * 4)
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef)}")
            # ORDER BY must be the distance alone: any extra sort key stops the
            # planner from using the HNSW index. Ties are broken below instead.
            rows = await (await conn.execute(
                f"SELECT chunk_id, text, metadata, 1 - (embedding <=> %s::vector) AS score FROM {t} "
                f"WHERE {clause} ORDER BY embedding <=> %s::vector LIMIT %s",
                [v, *params, v, top_k])).fetchall()
        out = [{"chunk_id": r[0], "text": r[1], "metadata": r[2], "score": float(r[3])} for r in rows]
        out.sort(key=lambda r: (-round(r["score"], 9), r["chunk_id"]))
        return out

    async def lexical_query(self, index_id: str, query: str, top_k: int, where=None) -> list[dict[str, Any]]:
        """Full-text retrieval over the tsv column, ranked by ts_rank with length
        normalisation. Same filter as vector queries."""
        if index_id in self._no_lexical:
            return []
        q = tsquery_text(query)
        if not q:
            return []
        t = self._table(index_id)
        clause, params = "TRUE", []
        if where is not None:
            if not hasattr(where, "sql"):
                raise StoreError("the pgvector store needs a RetrievalFilter, not an arbitrary callable")
            clause, params = where.sql()
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT chunk_id, text, metadata, ts_rank(tsv, q, 1) AS score "
                f"FROM {t}, to_tsquery(%s::regconfig, %s) q WHERE tsv @@ q AND {clause} "
                f"ORDER BY score DESC LIMIT %s", [self.fts_config, q, *params, top_k])).fetchall()
        out = [{"chunk_id": r[0], "text": r[1], "metadata": r[2], "score": float(r[3])} for r in rows]
        out.sort(key=lambda r: (-round(r["score"], 9), r["chunk_id"]))
        return out

    async def list_by_source_type(self, index_id: str, source_type: str) -> list[dict[str, Any]]:
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT chunk_id, text, metadata FROM {self._table(index_id)} "
                f"WHERE metadata->>'source_type' = %s", (source_type,))).fetchall()
        return [{"chunk_id": r[0], "text": r[1], "metadata": r[2]} for r in rows]

    async def get_items(self, index_id: str, chunk_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Text, metadata and vector of specific chunks."""
        if not chunk_ids:
            return {}
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT chunk_id, text, metadata, embedding::text FROM {self._table(index_id)} "
                f"WHERE chunk_id = ANY(%s)", (chunk_ids,))).fetchall()
        return {r[0]: {"chunk_id": r[0], "text": r[1], "metadata": r[2], "vector": _parse_vec(r[3])} for r in rows}

    async def explain(self, index_id: str, vector: list[float], top_k: int, where=None,
                      force_index: bool = False) -> str:
        """The plan for a query, so operations can confirm the HNSW index is used.
        On small tables the planner may rightly prefer a sequential scan;
        force_index=True shows whether the query shape can use the index at all."""
        t = self._table(index_id)
        clause, params = where.sql() if where is not None else ("TRUE", [])
        v = _vec(vector)
        async with self._conn() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {int(max(config.HNSW_EF_SEARCH, top_k * 4))}")
            if force_index:
                await conn.execute("SET LOCAL enable_seqscan = off")
            rows = await (await conn.execute(
                f"EXPLAIN SELECT chunk_id FROM {t} WHERE {clause} ORDER BY embedding <=> %s::vector LIMIT %s",
                [*params, v, top_k])).fetchall()
        return "\n".join(r[0] for r in rows)

    async def chunk_ids(self, index_id: str) -> set[str]:
        async with self._conn() as conn:
            rows = await (await conn.execute(f"SELECT chunk_id FROM {self._table(index_id)}")).fetchall()
        return {r[0] for r in rows}

    async def get_vectors(self, index_id: str, chunk_ids: list[str]) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT chunk_id, embedding::text FROM {self._table(index_id)} WHERE chunk_id = ANY(%s)",
                (chunk_ids,))).fetchall()
        return {r[0]: _parse_vec(r[1]) for r in rows}

    # ---------------------------------------------------------------- cache

    async def get_many(self, fingerprint: str, keys: list[str]) -> dict[str, list[float]]:
        if not keys:
            return {}
        async with self._conn() as conn:
            rows = await (await conn.execute(
                f"SELECT text_key, embedding FROM {self.schema}.embedding_cache "
                f"WHERE fingerprint = %s AND text_key = ANY(%s)", (fingerprint, keys))).fetchall()
        return {r[0]: _f32(r[1]) for r in rows}

    async def put_many(self, fingerprint: str, items: dict[str, list[float]]) -> None:
        if not items:
            return
        async with self._conn() as conn, conn.cursor() as cur:
            await cur.executemany(
                f"INSERT INTO {self.schema}.embedding_cache (fingerprint, text_key, embedding) VALUES (%s, %s, %s) "
                f"ON CONFLICT DO NOTHING", [(fingerprint, k, v) for k, v in items.items()])
