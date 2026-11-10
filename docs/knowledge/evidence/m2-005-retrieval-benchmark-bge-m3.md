# NEXA-M2-005 retrieval benchmark (gateway, pgvector)

Run 2026-11-10T11:16:14+00:00 on the DGX. v1 index embed-cand-bge-m3-cpu|bge-m3:567m-cpu|ollama|1024|bge-m3 (1811 chunks); v0 baseline embed-cand-nomic-embed-text-v1-5|nomic-embed-text:v1.5|ollama|768|nomic-embed (2310 v0 chunks). Gold set 40 English + 16 Tok Pisin.

| Config | EN R@1 | EN R@5 | TPI R@5 | MRR@10 EN | S | p50 ms | p95 ms | p95 excl. embed |
|---|---|---|---|---|---|---|---|---|
| v0_vector | 27.5 | 60.0 | 37.5 | 0.391 | 51.0 | 70.8 | 91.0 | - |
| v1_vector | 62.5 | 97.5 | 93.8 | 0.745 | 96.0 | 197.8 | 237.4 | 14.9 |
| v1_lexical | 57.5 | 82.5 | 68.8 | 0.698 | 77.0 | 5.8 | 8.9 | 8.9 |
| v1_hybrid | 57.5 | 92.5 | 68.8 | 0.72 | 83.0 | 195.5 | 231.6 | 17.4 |
| v1_hybrid_rerank | 62.5 | 100.0 | 87.5 | 0.77 | 95.0 | 203.5 | 257.0 | 27.9 |
| v1_hybrid_filtered | 57.5 | 95.0 | 68.8 | 0.721 | 84.5 | 190.8 | 245.2 | 17.0 |
| v1_hybrid_rerank_filtered | 60.0 | 100.0 | 87.5 | 0.754 | 95.0 | 200.0 | 246.3 | 26.4 |
| edge_hybrid | 57.5 | 92.5 | 68.8 | 0.718 | 83.0 | 195.0 | 242.6 | 18.9 |
| edge_hybrid_rerank | 62.5 | 100.0 | 87.5 | 0.775 | 95.0 | 198.5 | 245.7 | 19.5 |

| Comparison | ΔS | P(better) |
|---|---|---|
| v1_hybrid vs v0_vector | +32.0 | 1.0 |
| v1_vector vs v0_vector | +45.0 | 1.0 |
| v1_hybrid vs v1_vector | -13.0 | 0.0 |
| v1_hybrid_rerank vs v1_hybrid | +12.0 | 0.998 |
| v1_hybrid_filtered vs v1_hybrid | +1.5 | 0.638 |
| v1_hybrid_rerank vs v0_vector | +44.0 | 1.0 |
| edge_hybrid vs v1_hybrid | +0.0 | 0.0 |

**Reranker default:** on. reranker changes S by +12.0 (P(better) 0.998); the rule needs >= +1.0 with P >= 0.90 and no language losing recall@5.

**Evidence thresholds suggested:** min 0.507, strong 0.590739 (separation accuracy 0.984).
