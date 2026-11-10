# NEXA-M2-004 embedding evaluation (offline)

Run 2026-11-10T03:44:40+00:00; corpus 1811 chunks; gold set 40 English + 16 Tok Pisin questions; selection rule 1.0.0.

> **Self-test only.** Stand-in embedders, not real models. These numbers say nothing about model quality; they show the harness, gates and rule work end to end.

| Model | Role | Dim | Status | EN R@5 | TPI R@5 | Score | Trunc % | Query p50 ms | Gates | Eligible |
|---|---|---|---|---|---|---|---|---|---|---|
| nomic-embed-text:v1.5 | incumbent | 768 | ready | 42.5 | 25.0 | 35.5 | 0.0 | 0.2 | all pass | yes |
| nomic-embed-text-v2-moe | candidate | 768 | ready | 40.0 | 18.8 | 31.5 | 2.0 | 0.1 | all pass | yes |
| bge-m3 | candidate | 1024 | ready | 35.0 | 43.8 | 38.5 | 0.0 | 0.2 | all pass | yes |
| snowflake-arctic-embed2 | candidate | 1024 | ready | 55.0 | 31.2 | 45.5 | 0.0 | 0.2 | all pass | yes |
| embeddinggemma | candidate | 768 | ready | 42.5 | 50.0 | 45.5 | 0.0 | 0.2 | G4 | no |
| qwen3-embedding:0.6b | candidate | 1024 | ready | 22.5 | 0.0 | 13.5 | 0.0 | 0.2 | all pass | yes |
| granite-embedding:278m | candidate | 768 | ready | 55.0 | 43.8 | 50.5 | 1.8 | 0.1 | all pass | yes |
| mxbai-embed-large | control | 1024 | ready | 47.5 | 31.2 | 41.0 | 1.8 | 0.2 | all pass | no |
| all-minilm:33m | control | 384 | ready | 42.5 | 6.2 | 28.0 | 76.1 | 0.1 | G3 | no |
| qwen3-embedding:4b | reference | 2560 | ready | 20.0 | 6.2 | 14.5 | 0.0 | 0.4 | G2 | no |

**Selected baseline:** `embed-cand-granite-embedding-278m`. challenger beats incumbent by 15.0 points, P(better)=0.985

Best eligible challenger: `embed-cand-granite-embedding-278m` (margin 15.0, P(better) 0.985).
