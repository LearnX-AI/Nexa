# NEXA-M2-005: Hybrid Retrieval, Filtering and Reranking

Status: Implemented; real-model benchmark to run on the DGX (section 5)
Milestone: M2
Aligns with: NEXA-ADR-0002 (PostgreSQL + pgvector), NEXA-ADR-0004 (Knowledge Service), NEXA-M2-001 (authority), NEXA-M2-004 (index versions)
Implementation: `v1/knowledge/retrieval.py`, `access.py`, `authority.py` (`arbitrate`), `pgstore.py`, `store.py`; endpoint `POST /v1/knowledge/query`
Tool: `v1/tools/knowledge_retrieval_bench.py`
Tests: `v1/tests/test_knowledge_retrieval.py` (16), and the M2-004 and service suites, which now run through this pipeline
Evidence: `docs/knowledge/evidence/m2-005-*`

**Status against the acceptance criteria (section 9):**
- **Met:** filtering correctness, authority weighting with conflicting sources, latency targets documented, and API access.
- **Not yet evidenced with real models:** improvement over the v0 baseline. The benchmark harness runs end to end on pgvector here, but only with stand-in embedders, because the sandbox cannot reach a real embedding model. The DGX run produces the real numbers.

## 1. Pipeline

One request runs six stages. Each stage is timed and reported in the response.

| Stage | What happens |
|---|---|
| 1. Filters | The caller's access rules (approval, access class, effective dates, school scope) and the request's narrowing filters form one `RetrievalFilter`. Both retrievers apply it inside the query, so a passage the caller may not see is never a candidate, not even for fusion. |
| 2. Candidates | Vector (HNSW cosine) and lexical (PostgreSQL full-text) retrievers each return up to `candidates` passages (central 50, edge 20). |
| 3. Fusion | Reciprocal rank fusion: `sum(weight / (60 + rank))`. Cosine and `ts_rank` are on unrelated scales; RRF needs no calibration between them. |
| 4. Rerank (optional) | The top `rerank_pool` fused passages (central 30, edge 15) are re-scored. |
| 5. Authority | `authority.arbitrate()` applies the M2-001 rules to the top passages (section 4). |
| 6. Cut | The top `top_k` are returned with their stage scores, the reasons they were selected, and their provenance (M2-006). |

Modes: `hybrid` (default), `vector`, or `lexical`, per request or as `NEXA_RETRIEVAL_MODE`. If an index's lexical columns are unavailable, hybrid degrades to vector and the response says so (`retrieval.degraded`).

A result's `score` is its cosine similarity to the query. That is the same meaning `score` had in M1, so it can be compared across queries and used to judge evidence strength. Results come in final rank order, which is not always score order. For a passage found only by the lexical retriever, the similarity is computed from its stored vector.

## 2. Lexical retrieval

pgvector store:
- **Column.** Each index table gets a stored, generated `tsv` column with a GIN index.
- **What is indexed.** Body text is weighted A; the title and section path are weighted B.
- **Language configuration.** `english` (`NEXA_KNOWLEDGE_FTS_CONFIG`), which stems words and drops stop words. The corpus is English.

Queries:
- **Matching.** A query is the OR of the question's content words. A question is not a conjunction: requiring every word misses passages that answer it in other words. Ranking does the rest.
- **Ranking.** `ts_rank` with length normalisation.

The in-memory store, used for tests and development, uses BM25 with a small suffix stemmer. The two lexical backends rank similarly but not identically; the tests compare result sets, not orders.

Tok Pisin questions get little from the lexical retriever, because the curriculum is in English. They rely on the multilingual embedding model selected in M2-004.

## 3. Filters

### 3.1 Request filters

The request filters are `subjects`, `grades`, `languages`, `source_types`, `module_codes`, `topic_codes`, `scopes` and `max_tier`. For example, `max_tier: 3` restricts results to official material.

Rules:
- They only ever narrow what access allows; they cannot widen it.
- A field a filter names must be present on the passage. A passage without a subject never matches a subject filter, including documents from the M1 raw ingest endpoint, which carry no source contract.

### 3.2 Scope, by principal

The principal's school comes from a new token claim, `school_id`.

| Principal | National and provincial | School-scoped | Class-scoped |
|---|---|---|---|
| user with `school_id` X | yes | school X only | no (needs class membership, not issued yet) |
| user without a school | yes | no | no |
| administrator | yes | all | all |

### 3.3 Columns and migration

In pgvector, every filter runs on typed columns: `subject`, `grade_levels int[]`, `language`, `source_type`, `authority_tier`, `module_code`, `topic_code` and `school_id`.

- **Source of truth.** The columns are generated from the stored metadata, so writes are unchanged and the columns cannot disagree with the metadata.
- **Existing tables.** Tables created before M2-005 get the columns when the store opens. This rewrites the table once (seconds for the current corpus) and rebuilds its HNSW index.
- **Parity.** The Python filter and the SQL filter are tested for identical results on both stores, in all three modes, for every filter, role and school combination (`test_filtering_correctness_all_modes_and_stores`).

## 4. Authority: no silent override

`arbitrate()` applies the M2-001 rules at retrieval time:

- **Same point.** Two passages cover the same point when they share an outcome code or a topic code; failing codes, when they share the subject and module code. This is a structural test: NEXA does not try to decide whether two texts contradict each other, only which one is authoritative where both apply.
- **R4 and R5.** Within a point, a passage from a more authoritative source is moved directly above any less authoritative passage ranked higher. More authoritative means a lower tier number, or national scope over local scope within the same tier. The lower passage stays in the results, marked `authoritative: false`, so LMS and school material still supplements the official text but never replaces it. Each move is recorded on both passages and in `retrieval.conflicts`.
- **R3.** For a curriculum-outcome question ("learning outcomes", "syllabus", an outcome code), every tier-1 passage leads and only tier 1 is authoritative.
- **R7.** Permitted external sources (tier 6) go after every curriculum source and are marked external. They are used normally when nothing else covers the question.
- **Otherwise unchanged.** Passages on unrelated points keep their relevance order.

Tested scenarios (`test_authority_*`):
- LMS above the syllabus on the same topic;
- a school's teacher material above national LMS on a shared outcome;
- same tier, national and school scope;
- an outcome question;
- external alongside internal sources;
- unrelated passages left alone;
- the full pipeline, with the conflict visible in the explain payload.

## 5. Benchmark and the reranking decision

`knowledge_retrieval_bench.py` builds two indexes in a scratch schema:
- the **v1** index (v1 chunks, the chosen model);
- the **v0 baseline**: v0's own chunking (`v0_baseline.py`), embedded with v0's model (nomic-embed-text with prefixes) and queried by vector only, as `main.py` did.

It runs the gold set (40 English and 16 Tok Pisin questions) through nine configurations:
- the v0 baseline;
- v1 vector, lexical and hybrid;
- hybrid with reranking;
- hybrid, with and without reranking, using the subject and grade filters a tutor knows from the course;
- the edge profile, with and without reranking.

For each configuration it reports recall@1/5, MRR, the M2-004 score S, paired bootstrap comparisons, and stage latency.

**Reranker rule (fixed before the real run):** reranking is on by default only if it improves S by at least 1 point with P(better) ≥ 0.90 and lowers neither language's recall@5. Until the DGX run, `NEXA_RERANK` defaults to `off`.

**Results so far.** This sandbox run is a self-test with stand-in embedders. Only its lexical numbers measure anything real (`m2-005-retrieval-benchmark-offline.md`):

| Config | EN R@5 | TPI R@5 | S |
|---|---|---|---|
| v0 baseline | 30.0 | 12.5 | 23.0 |
| v1 lexical (real) | 82.5 | 68.8 | 77.0 |
| v1 hybrid | 75.0 | 50.0 | 65.0 |
| v1 hybrid + rerank | 90.0 | 50.0 | 74.0 |
| v1 hybrid, filtered | 85.0 | 62.5 | 76.0 |

**DGX run, to do.** Commit `m2-005-retrieval-benchmark.json` and `.md`, then fill in the following:
- **Final numbers:** *(table)*
- **v1 hybrid against v0:** ΔS and P(better)
- **Reranker default:** on or off, with ΔS and P(better)
- **RRF weights:** the setting the benchmark supports

## 6. Observability

`explain: true` adds a `retrieval` block to the response:
- the mode and profile;
- the filters applied, with no token contents;
- the claim class;
- the reranker;
- the index and embedding fingerprint;
- candidate counts per retriever;
- the authority conflicts;
- `timings_ms` per stage: embed, vector, lexical, fuse, similarity, rerank, authority, total.

Every result always carries:
- `scores`: vector and lexical score and rank, RRF, fused rank, similarity, rerank score and rank;
- `reasons`, for example: "found by vector rank 3 and lexical rank 1", "reranked from 4 to 2", "authority rules moved it from 5 to 2", "R4: ... is more authoritative on topic:12.1.2";
- `authority`;
- `coverage`.

The metric `nexa_knowledge_retrieval_ms_{count,sum}{mode}` is on `/metrics`.

## 7. Latency targets

Targets cover the retrieval call for 5 results, measured at the Knowledge Service.

| Profile | Hardware | Total p95 | Excluding query embedding p95 | Notes |
|---|---|---|---|---|
| central | DGX, pgvector, up to 100k chunks | ≤ 300 ms (nomic v1.5); ≤ 450 ms (bge-m3 on CPU) | ≤ 120 ms | The M2-004 measurement gives query embedding p95 of 83 ms (nomic) and 237 ms (bge-m3 on CPU). |
| edge | CPU-only school server, 8 GB (M2-004 E2), up to 50k chunks | ≤ 800 ms | ≤ 250 ms | Fewer candidates (20) and a smaller rerank pool (15). To be measured on the reference hardware in M2-009. |

Measured so far:

| Run | Configuration | Excluding embedding p95 | Stage p50 |
|---|---|---|---|
| Sandbox, pgvector 0.6, 1,811 chunks, stand-in embedder | hybrid | 29 ms | vector 7.3, lexical 6.2, similarity fetch 2.6, authority 0.3 ms |
| Same | hybrid + rerank | 43 ms | rerank 10.5 ms |
| Same | edge profile + rerank | 28 ms | |
| DGX | | *(run to fill)* | |

The live service before M2-005 measured 70 ms p50 and 84 ms p95 total, vector only, with nomic.

## 8. API

`POST /v1/knowledge/query`. The new fields are optional, so M1 callers are unchanged:

```json
{"query": "how do ionic bonds form", "top_k": 5,
 "filters": {"subjects": ["Chemistry"], "grades": [11], "max_tier": 3},
 "mode": "hybrid", "rerank": true, "profile": "central", "explain": true}
```

Each result adds `chunk_id`, `rank`, `scores`, `reasons`, `authority` and `provenance`. An invalid filter or mode returns 422.

## 9. Acceptance criteria (NEXA-M2-005)

| Criterion | Status | Evidence |
|---|---|---|
| Retrieval benchmark demonstrates measurable improvement over v0 baseline | **Harness done. Real-model run is on the DGX, not yet run.** The offline self-test shows the pipeline end to end, including the v0 baseline index built from v0's own chunking. | Section 5; `knowledge_retrieval_bench.py`; offline report |
| Filtering correctness tested | **Met** | `test_filtering_correctness_all_modes_and_stores` (both stores, three modes, 10 filters × 3 roles × 2 schools); `test_filter_rules_access_and_scope`; SQL/Python parity |
| Authority weighting tested with conflicting-source scenarios | **Met** | Section 4; seven `test_authority_*` scenarios, plus the conflict in the M2-006 answer test |
| Latency targets documented for central and edge profiles | **Met.** Targets set; measured in the sandbox; DGX and edge-hardware measurements to add | Section 7 |
| Retrieval pipeline is accessible through the NEXA Knowledge Service API | **Met** | `POST /v1/knowledge/query`; `test_query_api_filters_explain_and_validation` |

## 10. Limitations

- **No cross-encoder reranker.** The reranker is feature-based. Ollama does not serve cross-encoder rerankers, so a model reranker needs a gateway rerank endpoint first. `FeatureReranker` is the interface such a reranker would replace.
- **Same point is structural.** Arbitration recognises the same point by curriculum codes. Passages without codes, typically LMS material that teachers have not tagged, are arbitrated only within a shared subject and module.
- **Class scope unavailable.** Class-scoped content is invisible to everyone except administrators until identity issues class membership.
