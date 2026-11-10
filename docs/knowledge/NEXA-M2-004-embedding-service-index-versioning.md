# NEXA-M2-004: Embedding Service and Index Versioning

Status: Complete; baseline awaiting governance sign-off
Milestone: M2
Aligns with: NEXA-ADR-0001 (Model Gateway), NEXA-ADR-0002 (PostgreSQL + pgvector), NEXA-ADR-0004
Implementation: `v1/knowledge/embeddings.py`, `indexing.py`, `store.py`, `pgstore.py`,
`migrations/0002_vector_index.sql`, `benchmark/selection.py`; endpoints in `main.py`;
tools `knowledge_embed_eval.py`, `knowledge_index_drill.py`, `knowledge_index_bench.py`
Evidence: `docs/knowledge/evidence/m2-004-*`

**Status against the acceptance criteria (section 13):** all five are met.
Ten candidate models were evaluated through the gateway on the DGX, and the
selection rule picked **bge-m3 served on CPU** (`embed-cand-bge-m3-cpu`) as
the new baseline: score 94.5 against 77.5 for the incumbent, P(better) 0.998
(section 6.6). The re-index and rollback drill passed 11/11 with real models,
and the rollback was bit-identical (section 8). Switching the live index to
the new baseline is a separate, governed step (section 6.6).

## 1. What this adds

| Before (M1 to M2-003) | Now |
|---|---|
| In-memory vector list, lost on restart | pgvector store on PostgreSQL (ADR-0002), persistent; in-memory store kept for tests |
| Vectors carry no record of the model that made them | Every index version is stamped with a model **fingerprint**; vectors from two models are never compared |
| One implicit index | Versioned indexes: build beside the live one, validate, activate atomically, roll back, retire |
| Gateway model change goes unnoticed; scores silently become meaningless | Queries are refused (HTTP 503) when the gateway's model no longer matches the active index |
| nomic task prefixes dropped (regression from v0) | Prefixes restored, chosen from the model the gateway reports |
| Every re-index re-embeds everything | Content-addressed embedding cache: an unchanged corpus costs zero gateway calls |
| Re-indexing a source deletes, then re-adds | New chunks are written first, then stale ones removed: no gap in retrieval |

## 2. Embedding service

All embedding still goes through the Model Gateway (ADR-0001).

- **Model identity.** Each gateway reply carries `model_version` and `runtime`
  (from the gateway's model registry). With the vector dimension and the
  prefix scheme these make the fingerprint, e.g.
  `nexa-embed-default|nomic-embed-text|ollama|768|nomic-embed`.
- **Recorded with every index** (`knowledge.index_versions`, and as a comment on
  the index's own table): logical model, model name and version (the exact
  Ollama tag), runtime, dimension, prefix scheme, **tokenizer**, maximum input
  tokens, **normalisation** (`l2`), distance (`cosine`), index id, build stats
  and validation report. Tokenizer and input limit come from the model cards in
  `embeddings.MODEL_CARDS`.
- **Task prefixes.** Asymmetric retrieval models need different prefixes for
  documents and queries. v0 used them; its own comment notes that without them
  "retrieval returns the wrong subject" (main.py, `PrefixedOllamaEmbeddings`).
  The M1 client did not. Schemes are chosen from the reported model version:

  | Model family | Document prefix | Query prefix |
  |---|---|---|
  | nomic-embed | `search_document: ` | `search_query: ` |
  | e5, multilingual-e5 | `passage: ` | `query: ` |
  | bge, mxbai-embed | none | `Represent this sentence for searching relevant passages: ` |
  | anything else | none | none |

  `NEXA_EMBED_PREFIX_SCHEME` can force `none` or `custom`. The scheme is part
  of the fingerprint, so changing it requires a new index.
- **Validation.** Vectors must be finite and non-zero; they are L2-normalised.
  A different dimension is treated as a different model.
- **Batching.** `NEXA_EMBED_BATCH` texts per gateway call (default 32). One
  retry on gateway 5xx or connection errors; the gateway already retries its
  backend.
- **Cache.** Keyed by (fingerprint, SHA-256 of the prefixed text), stored in
  PostgreSQL (`knowledge.embedding_cache`). Queries are never cached: they are
  short, varied, and may contain personal wording.

## 3. Index versions

### 3.1 Lifecycle

```
           start_build              validation passes        activate
 (none) ───────────────► building ─────────────────► ready ──────────► active
                            │                          ▲                  │
                            │ validation fails         │ newer index      │
                            ▼                          │ activated        │
                          failed                       └──────────────────┘
                                                       retire (not active)
                                                 ready ──────────────────► retired
```

- Exactly one index is **active**; every query goes to it.
- Every write (an approved upload, a reprocessed run, a supersession) goes to
  the active index **and** to any index still building, so a build started
  before an upload does not miss it. Each target is written independently.
- **Activation** is one transaction that moves the `active` alias. The
  previous index becomes `ready` and is kept for rollback.
- **Rollback** first checks that the gateway still serves the old model, then
  catches the old index up on anything indexed since the switch, then
  activates it. It refuses if the old model is gone.
- **Retire** drops the index's table. The active index cannot be retired.
- The first upload on an empty store creates and activates a bootstrap index.

### 3.2 Validation (required before activation)

| Check | Rule |
|---|---|
| Completeness | Every active chunk of every active, approved source is present (`missing = 0`). |
| No stale content | Nothing from superseded, withdrawn or reprocessed-away chunks (`stale = 0`). |
| Self-retrieval | For a sample of up to 50 chunks, querying with the chunk's own text returns it in the top 5, for at least `NEXA_INDEX_SELF_RECALL_MIN` (0.8) of the sample. This catches a broken model, a wrong prefix scheme or a corrupt index. |
| Model | The gateway must currently serve the index's fingerprint. Checked even with `force`. |

Activation re-runs validation. `force: true` overrides the first three checks
(for an emergency, and it is audited), never the model check.

### 3.3 Query-time model check

Each query is embedded with the active index's logical model. If the gateway
reply's fingerprint differs from the index (for example, the gateway's
registry now maps `nexa-embed-default` to a new model), the query is refused
with **503** and the message tells the operator to build and activate a new
index. Comparing vectors from two models gives scores that look normal but are
meaningless, so refusing is the safe behaviour. The refusal is audited.

`GET /v1/knowledge/indexes/{id}?check_model=true` reports whether an index
still matches what the gateway serves.

## 4. pgvector store

| Object | Purpose |
|---|---|
| `knowledge.index_versions` | One row per index: status, fingerprint, build stats, validation report |
| `knowledge.index_alias` | The single `active` pointer |
| `knowledge.embedding_cache` | (fingerprint, text hash) to vector |
| `knowledge.vec_<index_id>` | One table per index: `vector(dim)` with an HNSW cosine index, the chunk text and metadata, and typed filter columns (approval, access class, scope, effective dates) |

- pgvector needs a fixed dimension to build an HNSW index, so each index
  version has its own table. Retiring an index drops its table cleanly.
- The access filter from M2-001 becomes a SQL `WHERE` clause on the typed
  columns, so filtering happens in the database.
- pgvector 0.6 (Ubuntu 24.04's package) filters *after* the HNSW scan, so
  filtered queries raise `hnsw.ef_search` to at least 4 × `top_k` (default 100).
  pgvector 0.8 adds iterative scans; the CI image uses 0.8.0.
- **The query must order by distance alone.** An extra sort key (for example a
  tie-breaker on `chunk_id`) stops PostgreSQL from using the HNSW index and
  falls back to a full scan. This was found during this work. Ties are now
  broken after the fetch, and a test checks the plan uses the index.
- Identifiers (schema, index ids) are validated against strict patterns
  before any DDL; values always go through query parameters.
- **Unchanged rows are never rewritten.** An upsert whose text, metadata and
  vector are identical leaves the row untouched. Every rewritten row becomes a
  new entry in the HNSW graph and shifts approximate results; the rollback
  drill (section 8) caught this when a rollback catch-up rewrote all of index A
  and only 47 of 56 gold questions got their old answers back. With the fix,
  56 of 56. Metadata changes (for example an access-class correction) are still
  applied.

## 5. API

All index endpoints need the new `knowledge.admin` permission, held by
`ai_engineer`, `admin` and `platform_admin`. Teachers cannot switch indexes.

| Endpoint | Purpose |
|---|---|
| `GET /v1/knowledge/indexes` | All index versions and the active one |
| `GET /v1/knowledge/indexes/{id}?check_model=` | One index; optionally compare with the gateway's current model |
| `POST /v1/knowledge/indexes` | Build a new index (`embed_model`, `activate_when_ready`, `note`). 202; runs in the background |
| `POST /v1/knowledge/indexes/{id}/validate` | Re-run validation |
| `POST /v1/knowledge/indexes/{id}/activate` | Activate a ready index (`force`) |
| `POST /v1/knowledge/indexes/rollback` | Return to the previous index |
| `DELETE /v1/knowledge/indexes/{id}` | Retire a non-active index |

`/health` now reports the active index and its fingerprint, and shows
`degraded` if the store is unreachable. Query responses report which index
answered (`x_nexa.index`).

## 6. Embedding model evaluation and baseline selection

### 6.1 Candidates

Chosen for being servable on the DGX through the gateway (Ollama), and for
multilingual or education suitability. Listed in
`v1/knowledge/benchmark/embedding_candidates.json`; the gateway registry
fragment is `v1/model_gateway/models.embedding-candidates.json`.

| Model (Ollama tag) | Role | Params | Dim | Input limit | Languages | Licence | Edge-feasible |
|---|---|---|---|---|---|---|---|
| nomic-embed-text:v1.5 | incumbent | 137M | 768 | 8,192 | English | Apache-2.0 | yes |
| nomic-embed-text-v2-moe | candidate | 475M (305M active) | 768 | 512 | ~100 | Apache-2.0 | yes |
| bge-m3 | candidate | 568M | 1024 | 8,192 | 100+ | MIT | yes |
| bge-m3, CPU-served (`bge-m3:567m-cpu`) | candidate | 568M | 1024 | 8,192 | 100+ | MIT | yes |
| snowflake-arctic-embed2 | candidate | 568M | 1024 | 8,192 | 74 | Apache-2.0 | yes |
| embeddinggemma | candidate | 308M | 768 | 2,048 | multilingual | Gemma Terms of Use | yes |
| qwen3-embedding:0.6b | candidate | 0.6B | 1024 | 32K | 100+ | Apache-2.0 | yes |
| granite-embedding:278m | candidate | 278M | 768 | 512 | multilingual | Apache-2.0 | yes |
| mxbai-embed-large | control (English) | 335M | 1024 | 512 | English | Apache-2.0 | yes |
| all-minilm:33m | control (smallest) | 33M | 384 | 256 | English | Apache-2.0 | yes |
| qwen3-embedding:4b | reference (upper bound) | 4B | 2560 | 40K | 100+ | Apache-2.0 | no |

The CPU-served entry is the same bge-m3 weights with the gateway option
`num_gpu: 0`, added after bge-m3 on the DGX GPU returned NaN for some inputs
(section 6.6). It has its own logical name and version label, so its vectors
get their own fingerprint and are never mixed with GPU-produced ones.

Sizes, dimensions and limits are from the publishers' model cards. The
evaluation records the dimension the model actually returns.

### 6.2 Benchmark

- **Corpus:** the 72-file NEXA curriculum corpus (1,811 chunks) from M2-002 and M2-003.
- **Questions:** the gold set, version 1.1.0: 40 English questions, plus 16 Tok
  Pisin questions that mirror English items. The Tok Pisin questions ask in Tok
  Pisin, while the answer is in the English curriculum, which is how PNG
  learners will often ask. The Tok Pisin wording is a draft by the engineering
  team and **must be reviewed by a Tok Pisin speaker** before the result is
  final. Hiri Motu items are still to be written.
- **Per model:** an index version built through the gateway (all ten in one
  database, side by side), validation, recall@1/5 and MRR@10 per language and
  per category, query-embedding latency, indexing throughput, and the share of
  chunks longer than the model's input limit. Lengths use the chunker's token
  estimate × 1.3, a conservative allowance for subword tokenizers. Measured on
  this corpus: 57.7% of chunks exceed 256 tokens, and 0.2% exceed 512.

### 6.3 Selection rule (fixed before any results; `benchmark/selection.py`, rule 1.0.0)

1. **Gates.** To be the baseline, a model must pass all of:
   - **G1 served:** its index built and validated through the gateway.
   - **G2 edge-feasible:** section 7.
   - **G3 context:** at most 5% of chunks truncated.
   - **G4 licence:** Apache-2.0 or MIT. Other licences need a governance decision first.
2. **Score.** S = 0.6 × English recall@5 + 0.4 × Tok Pisin recall@5.
3. **Incumbent advantage.** Re-embedding the corpus and every school edge has a
   cost. A challenger replaces the incumbent only if it wins by at least 3
   points of S *and* a paired bootstrap over questions gives P(better) ≥ 0.90.
4. **Near ties.** Among challengers within 2 points of the best, prefer the
   smaller dimension, then fewer parameters.

### 6.4 Self-test of the harness (stand-in models)

`python v1/tools/knowledge_embed_eval.py --corpus DIR --pg-dsn DSN --offline`
ran all ten candidates end to end on the real corpus in pgvector, in 82 s:
- **Indexes:** it built ten parallel index versions.
- **Gates:** each fired as designed. all-minilm failed G3 with 76% of chunks
  truncated. qwen3-embedding:4b failed G2. embeddinggemma failed G4.
- **Decision:** the rule produced one.

The embedders were deterministic stand-ins, so **the scores say nothing about
the real models**. Report: `docs/knowledge/evidence/m2-004-embedding-eval-offline-selftest.md`.

### 6.5 Running the evaluation on the DGX

```
for t in $(jq -r '.candidates[].ollama_tag' v1/knowledge/benchmark/embedding_candidates.json); do ollama pull $t; done
# merge v1/model_gateway/models.embedding-candidates.json into the gateway registry, reload the gateway
NEXA_KNOWLEDGE_GATEWAY_TOKEN=... python v1/tools/knowledge_embed_eval.py \
    --corpus DIR --pg-dsn "$NEXA_KNOWLEDGE_PG_DSN" --out docs/knowledge/evidence
```

This writes `m2-004-embedding-eval.json` and `.md`. The harness records a
failed question as a miss (and fails G1 if more than 5% fail) and keeps going,
so one model's error cannot stop the run; partial results are written to
`m2-004-embedding-eval.partial.json` after each model.

### 6.6 Selected baseline

**Selected: bge-m3, served on CPU through the gateway**
(`embed-cand-bge-m3-cpu`, version `bge-m3:567m-cpu`, 1024 dimensions,
XLM-RoBERTa SentencePiece tokenizer, L2-normalised, cosine).
Fingerprint: `embed-cand-bge-m3-cpu|bge-m3:567m-cpu|ollama|1024|bge-m3`.

#### Results on the DGX (gateway, real models, 1,811 chunks, gold set 1.1.0)

Two runs on 2026-11-10. **Run 1** evaluated all ten candidates. **Run 2**
re-ran the incumbent and both bge-m3 variants on the final harness. The
incumbent scored identically in both runs.

| Model | EN R@5 | TPI R@5 | Score S | Query p50 ms | Gates | Outcome |
|---|---|---|---|---|---|---|
| **bge-m3, CPU-served** (run 2) | **95.0** | **93.8** | **94.5** | 190 | all pass | **selected** |
| embeddinggemma (run 1) | 97.5 | 87.5 | 93.5 | | fails G4 (Gemma Terms of Use) | not eligible |
| bge-m3, GPU-served (runs 1 and 2) | 90.0 | 93.8 | 91.5 | 162 | all pass | 2 of 56 queries fail (NaN), see below |
| nomic-embed-text-v2-moe (run 1) | 97.5 | 75.0 | 88.5 | | all pass | |
| snowflake-arctic-embed2 (run 1) | 92.5 | 81.2 | 88.0 | | all pass | |
| qwen3-embedding:4b (run 1, reference) | 90.0 | 75.0 | 84.0 | | fails G2 (4B params, 2560-d) | not eligible |
| granite-embedding:278m (run 1) | 85.0 | 75.0 | 81.0 | | all pass | |
| qwen3-embedding:0.6b (run 1) | 92.5 | 62.5 | 80.5 | | all pass | |
| nomic-embed-text:v1.5 (incumbent, both runs) | 87.5 | 62.5 | 77.5 | 61 | all pass | replaced |
| mxbai-embed-large (run 1, control) | | | | | fails G1 (index did not build) | not eligible |
| all-minilm:33m (run 1, control) | | | | | fails G1, G3 (76% of chunks truncated) | not eligible |

S = 0.6 × English recall@5 + 0.4 × Tok Pisin recall@5. Report for run 2:
`docs/knowledge/evidence/m2-004-embedding-eval.json` and `.md`. The run 1
figures above are from its report on the DGX.

**Decision under rule 1.0.0:** the challenger beats the incumbent by 17.0
points with P(better) = 0.998, well past the 3-point and 0.90 thresholds. No
other eligible model is within 2 points, so the near-tie rule does not apply.

#### Rationale

1. **It retrieves the right passage far more often.** It finds the answer in
   the top 5 for 53 of 56 questions, against 45 for the incumbent. Of those
   questions, it finds 10 that the incumbent misses and misses 2 that the
   incumbent finds. Recall@1 rises from 52.5 to 60.0 in English and from 25.0
   to 50.0 in Tok Pisin.
2. **It works for questions asked in Tok Pisin**, which the English-only
   incumbent does not (93.8 against 62.5). That matters for how PNG learners
   will actually ask.
3. **The choice does not hang on the draft Tok Pisin questions.** On the 40
   English questions alone it still wins by 7.5 points (95.0 against 87.5).
   The Tok Pisin review can change the size of the margin, but is unlikely to
   change the decision.
4. **It passes every gate:**
   - MIT licence.
   - 568M parameters, within the edge limit.
   - 1024 dimensions, within E5.
   - An 8,192-token input limit, so no chunk is truncated.
   - Validation self-recall@5 of 0.94.
5. **It beats the strongest score among ineligible models.** embeddinggemma
   scored 93.5 but needs a licence decision. bge-m3 on CPU outscores it
   without one.

#### Why CPU-served, not GPU

On the DGX GPU, bge-m3 returns NaN for some inputs, and the gateway turns that
into a 502. This happened on the same questions (q18, q36) in both runs, so it
is reproducible, not transient. A model that intermittently gives a learner no
answer is not acceptable in service, even though 2 of 56 is under the G1 limit
of 5%.

The same weights served on CPU returned no errors. Their ranks match the GPU
run on every other question but one, where the rank moved from 5 to 4. The
failure is therefore in the GPU runtime path, not the model. The likely cause
is the Ollama/llama.cpp CUDA kernels on the GB10; the root cause is not
established.

The CPU path also matches the school edge, which is CPU-only (E2). The edge
must reproduce the central vectors exactly (E1, E6), so central and edge
should run the same runtime.

#### Costs accepted

| Cost | Measured | Assessment |
|---|---|---|
| Central indexing throughput | about 3 chunks/s on DGX CPU; 1,811 chunks in 10 min (GPU: 28/s) | Fine for today's corpus. A 100,000-chunk national corpus would take about 9 h to re-embed, acceptable for a planned re-index. Unchanged chunks come from the embedding cache. |
| Query embedding latency | p50 190 ms, p95 237 ms (incumbent: 61 and 83 ms) | Adds about 130 ms per question; small next to answer generation. Within the 300 ms edge target (E4), measured on DGX cores. School hardware must be measured in M2-009. |
| Storage | 4 KB per chunk at 1024-d (incumbent 3 KB) | 7.4 MB today; about 410 MB for 100,000 chunks |
| Re-embedding | one full central re-index, then shipping the new index to every edge | One-off; done with the blue/green build (section 3), with rollback kept |

If the GPU fault is fixed, for example by a newer Ollama, a GPU-served bge-m3
gets a new fingerprint. Moving to it is an ordinary re-index with the canary
check (E6), not a silent swap. The edges would stay on CPU, so the canary
check must confirm that GPU and CPU vectors agree before such a move.

#### Conditions and next steps

1. **Governance sign-off** on this baseline, recorded in this section.
2. **Tok Pisin review.** A Tok Pisin speaker reviews the 16 draft questions,
   then the evaluation is re-run (about 15 minutes). Point 3 above is why the
   decision is not expected to change.
3. **Switch the live index** with the blue/green procedure (section 11, step 5):
   - Register `embed-cand-bge-m3-cpu` in the production gateway registry.
   - `POST /v1/knowledge/indexes` with `embed_model: "embed-cand-bge-m3-cpu"`
     and `activate_when_ready: true`.
   - The current nomic index stays as `previous_index_id`, for rollback.
4. **Track the GPU NaN** as a runtime issue. It does not block the switch.

## 7. Central DGX and school-edge compatibility requirements

A vector is only meaningful next to vectors from the same model. In NEXA,
documents are embedded centrally, while a school server must embed its
learners' questions locally when it is offline. So **the edge must run exactly
the model the central index was built with.**

| Id | Requirement | How it is enforced |
|---|---|---|
| C1 | The central DGX (GB10, ARM64, CUDA, 128 GB unified memory) serves every embedding model through the Model Gateway only. | ADR-0001; the Knowledge Service has no embedding SDK |
| C2 | The gateway registry's `version` for an embedding model is the exact tag **including size and quantisation** (e.g. `nomic-embed-text:v1.5`, never `latest`). | Recorded in the fingerprint; operator rule |
| C3 | The central index for national content is built with the baseline model and is the source of truth. | Index lifecycle (section 3) |
| E1 | An edge server only serves an index whose fingerprint (model, version, quantisation, dimension, prefix scheme) its local runtime produces. | Same fingerprint check as the central 503 refusal; edge loader in M2-009 |
| E2 | Edge reference hardware: CPU only, x86_64 or ARM64, 8 GB RAM or more, no GPU, intermittent connectivity. *(Assumption; confirm with the school hardware plan in M2-009.)* | Planning input |
| E3 | Edge-feasible model: 600M parameters or fewer and 1.5 GB of RAM or less for the embedding runtime, available for the edge CPU architecture. | Selection gate G2 |
| E4 | Edge query embedding p95 of 300 ms or less on the reference hardware. Documents are never embedded at the edge; vectors come from the centre. | Measure on reference hardware in M2-009 |
| E5 | 1024 dimensions or fewer. pgvector can only build HNSW on `vector` up to 2,000 dimensions, so larger models (qwen3-embedding:4b at 2560) are central-only, or need `halfvec`. Storage is 4 bytes × dim per chunk: 1,811 chunks at 1024-d is 7.4 MB, and a 100,000-chunk national corpus is about 410 MB. | Gate G2; HNSW skipped above 2,000-d |
| E6 | Every index shipped to an edge carries a canary set: 20 gold questions and their central query vectors. Before serving, the edge embeds the same questions and requires cosine ≥ 0.999 with the central vectors, which catches runtime or quantisation drift that the fingerprint cannot see. | Requirement for M2-009 |
| E7 | Edges receive whole index versions and switch atomically, keeping the previous one for rollback, as the centre does. | Index lifecycle; M2-009 |

## 8. Re-index and rollback procedure, demonstrated

`v1/tools/knowledge_index_drill.py` runs the operational procedure against
PostgreSQL + pgvector on the real corpus and records a pass or fail for every
step. Here it ran with stand-in models: A at 768 dimensions, B at 1024.

| Step | Result |
|---|---|
| 1. Build and activate index A | pass. Validated: 0 missing, 0 stale, self-recall@5 1.0 |
| 2. Record the top-5 answers for all 56 gold questions | pass |
| 3a. Re-index one reprocessed source (new chunk config: 17 chunks became 23) | pass. Old chunks replaced; validation 0 missing, 0 stale |
| 3b. Full re-index of the corpus | pass. Idempotent: 1,817 chunks before and after, 0 gateway calls (cache) |
| 4. Build B in parallel while A keeps answering | pass. 2,232 queries answered by A during the build, 0 errors |
| 5. Activate B | pass. Queries answered by B; A kept as previous |
| 6a. A refuses vectors of B's dimension | pass |
| 6b. A query embedded with model B is refused by index A | pass (model-changed error) |
| 7. Roll back to A | pass. **56/56** gold questions get identical top-5 answers to step 2 |
| 8. Retire B; the active index cannot be retired | pass |

Transcript: `docs/knowledge/evidence/m2-004-reindex-rollback-drill-offline.json`.

### 8.1 With real models on the DGX

On 2026-11-10 the drill ran through the gateway:
- A: `nomic-embed-text:v1.5`, 768-d.
- B: `nomic-embed-text-v2-moe`, 768-d.
- Same corpus, 1,817 chunks after the step-3 reprocess.

**It passed all 11 steps.**

| Step | Result |
|---|---|
| Build and activate A | validated: 0 missing, 0 stale, self-recall@5 0.90 |
| Re-index one reprocessed source | 17 chunks became 23; 0 missing, 0 stale |
| Full re-index | 1,817 chunks before and after, **0 gateway calls** |
| Build B while A serves | 731 queries answered by A during the 51 s build, 0 errors |
| Mixing prevention | the models share a dimension, so this rests on the fingerprint check. A query embedded with B against A is refused with "embedding model changed". |
| Roll back to A | **bit-identical**: the same SHA-256 over every chunk ID and vector before and after (`dcd92a94…`) |
| Answers after rollback | **56/56** gold questions get the same top 5 |
| Retire B; the active index is protected | pass |

Transcript: `docs/knowledge/evidence/m2-004-reindex-rollback-drill.json`.

Bit-identical rollback with a real model depends on two things (PR #88):
- Vectors are quantised to float32 when embedded, and written and read back
  at 9 significant digits, so a cached vector equals the stored one.
- An upsert with unchanged content rewrites nothing.

Before that change, a real-model rollback gave 51/56 identical answers.

The same steps are also covered by `test_blue_green_build_activate_rollback_retire`,
which runs on every CI build against both stores.

## 9. Store and index results

### 9.1 Real corpus in pgvector (offline embedder)

Run with `python v1/tools/knowledge_index_bench.py --corpus DIR --pg-dsn DSN --offline`
on the 72-file corpus from M2-002 (1,811 chunks), PostgreSQL 16 with pgvector
0.6.0, 768-dimension vectors from a deterministic local hashing embedder
(this sandbox cannot reach a real embedding model).

| Measure | Result |
|---|---|
| Build: chunks indexed, time, gateway calls | 1,811 in 5.4 s, 105 calls (batches of up to 32, per source, across 72 sources) |
| Validation | passed: 0 missing, 0 stale, self-recall@5 = 1.00 on 50 samples |
| Rebuild of the unchanged corpus | 0 document gateway calls, 1,811 cache hits |
| HNSW vs exact search, top-10 overlap (100 queries) | mean 0.981, worst 0.80 |
| Filtered student query latency | p50 1.9 ms, p95 2.4 ms |
| Query plan uses the HNSW index | yes |

These numbers measure the store and the index lifecycle. They say nothing
about semantic quality, because the offline embedder has no semantics.

### 9.2 On the DGX, with real models

The first dense-retrieval baselines for M2-005 and M2-008 are the run-2
results in section 6.6:
- the incumbent at EN recall@5 87.5;
- the new baseline at 95.0.

The live v1 Knowledge Service, still on the incumbent, was verified after
deployment with the following results:
- 1,811 chunks, 0 missing, 0 stale, self-recall 0.94.
- The same recall as the evaluation.
- Query p50 70 ms, p95 84 ms.
- 0 teacher-only results returned to students.
- Every result cited.

## 10. Tests

`v1/tests/test_knowledge_indexing.py` (24 tests) and `test_embedding_selection.py` (8 tests). The store contract and the
index lifecycle run against both backends: the in-memory store always, and
PostgreSQL + pgvector when `NEXA_TEST_PG_DSN` is set. CI starts
`pgvector/pgvector:0.8.0-pg16` and fails if the pgvector tests skip. Covered:

- prefixes, normalisation, batching, cache, vector validation, model-change detection
- filter parity between Python and SQL
- store contract: upsert without duplicates, dimension check, filtered query,
  alias switching, retire, cache, persistence across reopen, HNSW plan used,
  unsafe identifiers rejected
- full blue/green lifecycle with a gateway model change mid-way; validation
  blocking an incomplete index; reprocess and supersede keeping the index exact;
  an unchanged rebuild using only the cache
- endpoints and RBAC, the 503 refusal, M1 raw ingest still working and idempotent

## 11. Operations

1. Install pgvector: `apt install postgresql-16-pgvector` (Ubuntu 24.04 ships 0.6.0; 0.5 is the minimum).
2. As an administrator: `psql -v svc_role=nexa_knowledge -f v1/knowledge/migrations/0002_vector_index.sql "$ADMIN_DSN"`.
3. In `/etc/nexa/knowledge.env`: `NEXA_KNOWLEDGE_STORE=pgvector` and
   `NEXA_KNOWLEDGE_PG_DSN`; the password goes in `.pgpass` or `PGPASSWORD`
   there, never in the repository. Set `NEXA_KNOWLEDGE_REINDEX_ON_START=false`.
4. Restart the service. The first approved upload, or
   `POST /v1/knowledge/sources/all/reindex`, creates the bootstrap index.
5. **Changing the embedding model:** point the gateway's logical model at the
   new backend. Queries return 503 until a matching index is active. Then
   `POST /v1/knowledge/indexes` with `activate_when_ready: true`. To keep
   serving during the change, register the new model under a second logical
   name, build with `embed_model` set to it, then switch the gateway mapping
   and activate.

## 12. Known limitations

- The registry (sources, jobs, chunks) is still SQLite. Its PostgreSQL adapter
  (DDL in `0001_knowledge_registry.sql`) is a separate step.
- Filtered HNSW on pgvector 0.6 can return fewer than `top_k` results when a
  filter excludes most of the corpus. The raised `ef_search` mitigates this;
  pgvector 0.8 iterative scans fix it.
- Index builds run inside the service process. A multi-worker deployment
  should run builds from one worker or a job runner.

## 13. Acceptance criteria (NEXA-M2-004)

| Criterion | Status | Evidence |
|---|---|---|
| Candidate embedding models evaluated on NEXA curriculum benchmark | **Met.** Ten candidates, plus a CPU-served variant of bge-m3, evaluated through the gateway on the DGX, on 1,811 chunks with 40 English and 16 Tok Pisin questions | Section 6.6; `m2-004-embedding-eval.json` and `.md` |
| Selected baseline documented with rationale | **Met, pending governance sign-off.** bge-m3, CPU-served: S 94.5 against 77.5, P(better) 0.998, under rule 1.0.0 fixed before any results | Section 6.6 |
| Embedding/index version stored with every vector collection/index | **Met** | Section 2; index record plus a comment on each vector table; tests |
| Re-index procedure tested | **Met** | Section 8 steps 3a and 3b; tests on both stores in CI |
| Rollback to previous index demonstrated | **Met, with real models.** On the DGX the drill passed 11/11 with nomic v1.5 and v2-moe: rollback bit-identical, 56/56 identical answers | Section 8.1; `m2-004-reindex-rollback-drill.json` |

Requirements: model independence (sections 2 and 3), the recorded fields
(section 2), controlled re-embedding and parallel index versions (sections 3
and 8), no silent mixing (sections 3.3, 4 and 8, step 6a/6b), and central and edge
compatibility (section 7).
