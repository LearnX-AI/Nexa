# NEXA-M2-004 embedding evaluation (gateway)

Run 2026-11-10T09:43:45+00:00; corpus 1811 chunks; gold set 40 English + 16 Tok Pisin questions; selection rule 1.0.0.

| Model | Role | Dim | Status | EN R@5 | TPI R@5 | Score | Trunc % | Query p50 ms | Gates | Eligible |
|---|---|---|---|---|---|---|---|---|---|---|
| nomic-embed-text:v1.5 | incumbent | 768 | ready | 87.5 | 62.5 | 77.5 | 0.0 | 61.2 | all pass | yes |
| bge-m3 | candidate | 1024 | ready | 90.0 | 93.8 | 91.5 | 0.0 | 162.3 | all pass | yes |
| bge-m3 | candidate | 1024 | ready | 95.0 | 93.8 | 94.5 | 0.0 | 190.3 | all pass | yes |

**Selected baseline:** `embed-cand-bge-m3-cpu`. challenger beats incumbent by 17.0 points, P(better)=0.998

Best eligible challenger: `embed-cand-bge-m3-cpu` (margin 17.0, P(better) 0.998).
