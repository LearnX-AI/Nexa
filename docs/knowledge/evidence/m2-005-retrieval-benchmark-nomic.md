# NEXA-M2-005 retrieval benchmark (gateway, pgvector)

Run 2026-11-10T11:14:17+00:00 on the DGX. v1 index embed-cand-nomic-embed-text-v1-5|nomic-embed-text:v1.5|ollama|768|nomic-embed (1811 chunks); v0 baseline embed-cand-nomic-embed-text-v1-5|nomic-embed-text:v1.5|ollama|768|nomic-embed (2310 v0 chunks). Gold set 40 English + 16 Tok Pisin.

| Config | EN R@1 | EN R@5 | TPI R@5 | MRR@10 EN | S | p50 ms | p95 ms | p95 excl. embed |
|---|---|---|---|---|---|---|---|---|
| v0_vector | 27.5 | 60.0 | 37.5 | 0.391 | 51.0 | 74.8 | 91.3 | - |
| v1_vector | 60.0 | 90.0 | 62.5 | 0.705 | 79.0 | 78.6 | 99.0 | 13.5 |
| v1_lexical | 57.5 | 82.5 | 68.8 | 0.698 | 77.0 | 5.9 | 8.2 | 8.2 |
| v1_hybrid | 50.0 | 97.5 | 81.2 | 0.685 | 91.0 | 71.8 | 96.2 | 14.8 |
| v1_hybrid_rerank | 65.0 | 95.0 | 75.0 | 0.769 | 87.0 | 87.9 | 111.0 | 30.0 |
| v1_hybrid_filtered | 50.0 | 97.5 | 81.2 | 0.681 | 91.0 | 78.4 | 98.4 | 17.2 |
| v1_hybrid_rerank_filtered | 65.0 | 95.0 | 68.8 | 0.764 | 84.5 | 84.7 | 110.5 | 25.0 |
| edge_hybrid | 50.0 | 97.5 | 81.2 | 0.685 | 91.0 | 76.8 | 103.5 | 18.7 |
| edge_hybrid_rerank | 65.0 | 95.0 | 87.5 | 0.769 | 92.0 | 86.8 | 102.9 | 20.5 |

| Comparison | ΔS | P(better) |
|---|---|---|
| v1_hybrid vs v0_vector | +40.0 | 1.0 |
| v1_vector vs v0_vector | +28.0 | 1.0 |
| v1_hybrid vs v1_vector | +12.0 | 0.994 |
| v1_hybrid_rerank vs v1_hybrid | -4.0 | 0.0 |
| v1_hybrid_filtered vs v1_hybrid | +0.0 | 0.0 |
| v1_hybrid_rerank vs v0_vector | +36.0 | 1.0 |
| edge_hybrid vs v1_hybrid | +0.0 | 0.346 |

**Reranker default:** off. reranker changes S by -4.0 (P(better) 0.0); the rule needs >= +1.0 with P >= 0.90 and no language losing recall@5.

**Evidence thresholds suggested:** min 0.592, strong 0.69234 (separation accuracy 0.882).
