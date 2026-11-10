# NEXA-M2-005 retrieval benchmark (offline, pgvector)

Run 2026-11-10T10:27:29+00:00. v1 index nexa-embed-default|nomic-embed-text:v1.5|offline-standin|768|nomic-embed (1811 chunks); v0 baseline nexa-embed-default|nomic-embed-text:v1.5|offline-standin|768|nomic-embed (2310 v0 chunks). Gold set 40 English + 16 Tok Pisin.

> **Stand-in embedders.** Vector and hybrid numbers here say nothing about real models; lexical numbers are real. Run on the DGX for the evidence.

| Config | EN R@1 | EN R@5 | TPI R@5 | MRR@10 EN | S | p50 ms | p95 ms | p95 excl. embed |
|---|---|---|---|---|---|---|---|---|
| v0_vector | 17.5 | 30.0 | 12.5 | 0.238 | 23.0 | 3.1 | 4.4 | - |
| v1_vector | 17.5 | 40.0 | 31.2 | 0.281 | 36.5 | 12.5 | 21.1 | 20.5 |
| v1_lexical | 57.5 | 82.5 | 68.8 | 0.698 | 77.0 | 10.9 | 16.3 | 16.3 |
| v1_hybrid | 37.5 | 75.0 | 50.0 | 0.537 | 65.0 | 22.2 | 29.2 | 28.7 |
| v1_hybrid_rerank | 45.0 | 90.0 | 50.0 | 0.622 | 74.0 | 32.3 | 43.1 | 42.7 |
| v1_hybrid_filtered | 47.5 | 85.0 | 62.5 | 0.619 | 76.0 | 20.3 | 28.1 | 27.7 |
| v1_hybrid_rerank_filtered | 50.0 | 92.5 | 56.2 | 0.663 | 78.0 | 32.7 | 47.9 | 47.5 |
| edge_hybrid | 40.0 | 77.5 | 50.0 | 0.549 | 66.5 | 20.2 | 31.3 | 30.7 |
| edge_hybrid_rerank | 45.0 | 87.5 | 50.0 | 0.631 | 72.5 | 19.8 | 28.0 | 27.6 |

| Comparison | ΔS | P(better) |
|---|---|---|
| v1_hybrid vs v0_vector | +42.0 | 1.0 |
| v1_vector vs v0_vector | +13.5 | 0.998 |
| v1_hybrid vs v1_vector | +28.5 | 1.0 |
| v1_hybrid_rerank vs v1_hybrid | +9.0 | 0.966 |
| v1_hybrid_filtered vs v1_hybrid | +11.0 | 0.997 |
| v1_hybrid_rerank vs v0_vector | +51.0 | 1.0 |
| edge_hybrid vs v1_hybrid | +1.5 | 0.637 |

**Reranker default:** on. reranker changes S by +9.0 (P(better) 0.966); the rule needs >= +1.0 with P >= 0.8 and no language losing recall@5.

**Evidence thresholds suggested:** min 0.311, strong 0.311 (separation accuracy 0.778).

