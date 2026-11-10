# NEXA-M2-005: Hybrid Retrieval, Filtering and Reranking

Status: Complete. Benchmarked with real models on the DGX, 2026-11-10 (section 5).
Milestone: M2
Aligns with: NEXA-ADR-0002 (PostgreSQL + pgvector), NEXA-ADR-0004 (Knowledge Service), NEXA-M2-001 (authority), NEXA-M2-004 (index versions)
Implementation: `v1/knowledge/retrieval.py`, `access.py`, `authority.py` (`arbitrate`), `pgstore.py`, `store.py`; endpoint `POST /v1/knowledge/query`
Tool: `v1/tools/knowledge_retrieval_bench.py`
Tests: `v1/tests/test_knowledge_retrieval.py` (16), and the M2-004 and service suites, which now run through this pipeline
Evidence: `docs/knowledge/evidence/m2-005-*`

**Status against the acceptance criteria (section 9):** all five are met. On the DGX with real models, v1 retrieval beats the v0 baseline by 40 points of S with the live model (nomic v1.5: 91.0 against 51.0) and by 45 with the selected model (bge-m3 on CPU: 96.0). The best configuration differs by model, so the default follows the active index's model (section 5).

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

Modes: `hybrid`, `vector`, or `lexical`, per request or as `NEXA_RETRIEVAL_MODE`. The default, `auto`, picks the mode the benchmark found best for the active index's embedding model (section 5). If an index's lexical columns are unavailable, hybrid degrades to vector and the response says so (`retrieval.degraded`).

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

**Reranker rule (fixed before the real run):** reranking is on by default only if it improves S by at least 1 point with P(better) ≥ 0.90 and lowers neither language's recall@5.

### 5.1 Results on the DGX (2026-11-10)

Gateway, real models, pgvector, 1,811 v1 chunks and 2,310 v0 chunks. The v0 baseline is nomic v1.5 on v0's chunks, vector only. Reports: `evidence/m2-005-retrieval-benchmark-nomic.md` and `-bge-m3.md`.

| Config | nomic v1.5 (live): EN R@5 / TPI R@5 / **S** | bge-m3 on CPU (selected in M2-004): EN R@5 / TPI R@5 / **S** |
|---|---|---|
| v0 baseline | 60.0 / 37.5 / **51.0** | 60.0 / 37.5 / **51.0** |
| v1 vector | 90.0 / 62.5 / **79.0** | 97.5 / 93.8 / **96.0** |
| v1 lexical | 82.5 / 68.8 / **77.0** | 82.5 / 68.8 / **77.0** |
| v1 hybrid | 97.5 / 81.2 / **91.0** | 92.5 / 68.8 / **83.0** |
| v1 hybrid + rerank | 95.0 / 75.0 / **87.0** | 100.0 / 87.5 / **95.0** |
| v1 hybrid, filtered | 97.5 / 81.2 / **91.0** | 95.0 / 68.8 / **84.5** |
| edge profile, hybrid | 97.5 / 81.2 / **91.0** | 92.5 / 68.8 / **83.0** |

**Improvement over v0:**
- **nomic, hybrid:** +40.0, P(better) 1.0.
- **bge-m3, vector:** +45.0, P(better) 1.0.

**What the two models show:**
- **nomic, which is English-only, gains from keyword matches.** Hybrid beats vector by 12.0 (P 0.994), mostly through Tok Pisin questions, whose English terms (DNA, enzyme names) the lexical retriever finds.
- **bge-m3 gains nothing from them.** It is multilingual and already strong alone. Equal-weight fusion lets English keyword matches outrank its correct cross-lingual matches: Tok Pisin top-5 falls from 93.8 to 68.8 and S drops by 13.0. Reranking repairs most of that (+12.0, P 0.998, so the rule says on for bge-m3 hybrid), but not past vector alone.
- **Filters** made no difference on this corpus. Hybrid already finds the right subject; the filters matter for access, scope and narrowing.
- **The edge profile**, with fewer candidates, scored the same as central.

### 5.2 Decision: the default follows the model

`NEXA_RETRIEVAL_MODE=auto` and `NEXA_RERANK=auto` take the defaults per embedding model (`config.RETRIEVAL_DEFAULTS`). The rule: the configuration with the highest S; within 2 points of the best (the M2-004 near-tie rule), the simpler and faster one.

| Model | Default | Why |
|---|---|---|
| nomic-embed v1.5 | hybrid, no rerank | hybrid 91.0 is best; rerank lowers S by 4.0 (fails the reranker rule) |
| bge-m3 | vector, no rerank | vector 96.0 is best; hybrid + rerank 95.0 is within 2 points and is slower |
| other models | hybrid, no rerank | until benchmarked |

When the live index switches to bge-m3 (M2-004 section 6.6), retrieval switches to vector only with it, with no configuration change.

**Follow-up for M2-008:**
- **Tuning.** Lower lexical fusion weight (`NEXA_RRF_WEIGHT_LEXICAL`) for multilingual models, and language-aware fusion (lexical only for English questions), are the obvious things to tune.
- **The cost of hybrid.** It places the right passage first less often than vector alone with nomic (R@1 50.0 against 60.0). This is acceptable for grounded answers, which use the top 5, and worth tuning.

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
| DGX, real models, 1,811 chunks | nomic hybrid (live default) | 14.8 ms (total p95 96 ms) | |
| Same | bge-m3 vector (selected default) | 14.9 ms (total p95 237 ms; query embedding on CPU) | |
| Same | edge profile, nomic hybrid | 18.7 ms (total p95 104 ms) | |

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
| Retrieval benchmark demonstrates measurable improvement over v0 baseline | **Met.** On the DGX, S rose from 51.0 (v0) to 91.0 with nomic hybrid (+40.0, P 1.0) and to 96.0 with bge-m3 vector (+45.0, P 1.0). English top-5 rose from 60.0 to 97.5; Tok Pisin from 37.5 to 81.2 and 93.8. | Section 5; `evidence/m2-005-retrieval-benchmark-nomic.md`, `-bge-m3.md` |
| Filtering correctness tested | **Met** | `test_filtering_correctness_all_modes_and_stores` (both stores, three modes, 10 filters × 3 roles × 2 schools); `test_filter_rules_access_and_scope`; SQL/Python parity |
| Authority weighting tested with conflicting-source scenarios | **Met** | Section 4; seven `test_authority_*` scenarios, plus the conflict in the M2-006 answer test |
| Latency targets documented for central and edge profiles | **Met.** Targets set, and central measured on the DGX within them (p95 96 ms nomic, 237 ms bge-m3). Edge-hardware measurement is in M2-009. | Section 7 |
| Retrieval pipeline is accessible through the NEXA Knowledge Service API | **Met** | `POST /v1/knowledge/query`; `test_query_api_filters_explain_and_validation` |

## 10. Limitations

- **No cross-encoder reranker.** The reranker is feature-based. Ollama does not serve cross-encoder rerankers, so a model reranker needs a gateway rerank endpoint first. `FeatureReranker` is the interface such a reranker would replace.
- **Same point is structural.** Arbitration recognises the same point by curriculum codes. Passages without codes, typically LMS material that teachers have not tagged, are arbitrated only within a shared subject and module.
- **Class scope unavailable.** Class-scoped content is invisible to everyone except administrators until identity issues class membership.
