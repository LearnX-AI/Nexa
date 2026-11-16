# NEXA-M2-008 reproduction check on a second machine (harness), 2026-11-16

Machine: a clean cloud Linux container (not the DGX), Python 3.10, PostgreSQL 16 with pgvector, Tesseract,
no Ollama. Commit `3049cb9` plus the reporting changes in this pull request. Procedure:
[NEXA-M2-008-reproduce.md](../NEXA-M2-008-reproduce.md).

| Step | Result |
|---|---|
| Fixture suite (section 2) | Gate PASS, 25/25 checks (`m2-008-eval-fixture-20261116.md`) |
| Corpus build from the repository's curriculum files | 72 sources, 1,811 chunks: identical to the DGX runs of 2026-11-11 and 2026-11-15 |
| Corpus suite, `--offline` stand-in embedders | Ran end to end in 66 s: both indexes built in pgvector, all 80 corpus items scored and sliced, report written. Gate FAIL by design (stand-ins can never pass) |

Not reproduced here: the real-model numbers (no Ollama in this container). Those need section 3 of the
procedure on a machine with Ollama. On the DGX, the runs of 2026-11-11 (`0f43e0e`) and 2026-11-15
(`9b6c06c`) both measured gating recall@5 95.8.
