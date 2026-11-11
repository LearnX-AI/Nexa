# NEXA-M2-008 evaluation: corpus suite

Dataset 1.1.0, thresholds 1.1.0. Run 2026-11-11T13:00:58+00:00, commit `0f43e0e72a46`, gate key `7e5a35deae7c1454`.

Index nexa-embed-default|nomic-embed-text|ollama|768|nomic-embed, built in 33.0 s (72 sources, 1811 chunks). Store pgvector.

**Gate: FAIL**

| Check | Value | Limit | Result |
|---|---|---|---|
| retrieval.gating.recall@5 | 95.8 | >= 85.0 | pass |
| retrieval.gating.mrr@10 | 0.706 | >= 0.6 | pass |
| retrieval.by_type.exact_fact.recall@5 | 95.0 | >= 85.0 | pass |
| retrieval.by_type.conceptual.recall@5 | 100.0 | >= 85.0 | pass |
| retrieval.by_type.ambiguous.recall@5 | 87.5 | >= 75.0 | pass |
| authority.syllabus_first_rate | 1.0 | >= 0.8 | pass |
| no_answer.correct_refusal_rate | 0.5 | >= 0.75 | **FAIL** |
| no_answer.false_refusal_rate | 0.0 | <= 0.1 | pass |
| latency.p95_ms | 104.5 | <= 450.0 | pass |

## Retrieval

| Set | n | recall@1 | recall@5 | recall@10 | mrr@10 | ndcg@10 | precision@5 |
|---|---|---|---|---|---|---|---|
| gating (approved, EN) | 48 | 54.2 | 95.8 | 97.9 | 0.706 | 0.774 | 0.633 |
| v0 baseline, gating | 48 | 27.1 | 56.2 | 58.3 | 0.373 | 0.424 | 0.583 |
| type=exact_fact | 20 | 55.0 | 95.0 | 100.0 | 0.695 | 0.769 | 0.6 |
| type=conceptual | 20 | 45.0 | 100.0 | 100.0 | 0.675 | 0.757 | 0.64 |
| type=ambiguous | 8 | 75.0 | 87.5 | 87.5 | 0.812 | 0.829 | 0.7 |
| type=multilingual_unapproved | 16 | 37.5 | 81.2 | 87.5 | 0.495 | 0.585 | 0.588 |
| subject=Biology | 27 | 44.4 | 92.6 | 96.3 | 0.637 | 0.718 | 0.667 |
| subject=Chemistry | 9 | 66.7 | 100.0 | 100.0 | 0.806 | 0.855 | 0.622 |
| subject=Physics | 12 | 66.7 | 100.0 | 100.0 | 0.787 | 0.84 | 0.567 |
| grade=11 | 2 | 50.0 | 100.0 | 100.0 | 0.75 | 0.815 | 0.7 |
| grade=12 | 46 | 54.3 | 95.7 | 97.8 | 0.704 | 0.772 | 0.63 |
| term=1 | 9 | 55.6 | 100.0 | 100.0 | 0.744 | 0.809 | 0.578 |
| term=2 | 7 | 85.7 | 100.0 | 100.0 | 0.929 | 0.947 | 0.629 |
| term=3 | 8 | 50.0 | 100.0 | 100.0 | 0.688 | 0.765 | 0.55 |
| term=None | 24 | 45.8 | 91.7 | 95.8 | 0.633 | 0.714 | 0.683 |
| language=en | 48 | 54.2 | 95.8 | 97.9 | 0.706 | 0.774 | 0.633 |
| format=docx | 23 | 47.8 | 91.3 | 95.7 | 0.639 | 0.717 | 0.687 |
| format=pdf | 24 | 62.5 | 100.0 | 100.0 | 0.779 | 0.835 | 0.583 |
| format=pptx | 1 | 0.0 | 100.0 | 100.0 | 0.5 | 0.631 | 0.6 |
| source_type=lms_content | 23 | 47.8 | 91.3 | 95.7 | 0.639 | 0.717 | 0.687 |
| source_type=official_syllabus | 9 | 100.0 | 100.0 | 100.0 | 1.0 | 1.0 | 0.578 |
| source_type=student_guide | 12 | 41.7 | 100.0 | 100.0 | 0.642 | 0.731 | 0.6 |
| source_type=teacher_guide | 3 | 33.3 | 100.0 | 100.0 | 0.667 | 0.754 | 0.533 |
| source_type=teacher_material | 1 | 0.0 | 100.0 | 100.0 | 0.5 | 0.631 | 0.6 |

v1 minus v0 (gating set): recall@1 +27.1, recall@5 +39.6, recall@10 +39.6, mrr@10 +0.333, ndcg@10 +0.35, precision@5 +0.05.

## Authority, refusal, latency

- Syllabus-defined questions with an official syllabus passage first: 1.0 (n=6)
- Unanswerable questions refused (evidence none): 0.5 (n=16); answerable questions wrongly refused: 0.0 (n=48)
- Retrieval latency p50 78.1 ms, p95 104.5 ms

## Misses

- q14-tpi (multilingual): expected passage not in the top 10; top was STEMPhysics-Term3-StudentGuide-Gr12.pdf
- q37-tpi (multilingual): expected passage not in the top 10; top was s23_cm8936_12.6_Health and Medical_1_Homeostasis (Concept  Mechanisms).docx
- a04 (ambiguous): expected passage not in the top 10; top was s01_cm8870_12.1_Sciencetific Methods and Skills_Lesson Notes.docx
- u03 (no_answer): not refused, evidence weak
- u06 (no_answer): not refused, evidence weak
- u09 (no_answer): not refused, evidence weak
- u10 (no_answer): not refused, evidence weak
- u11 (no_answer): not refused, evidence weak
- u12 (no_answer): not refused, evidence strong
- u13 (no_answer): not refused, evidence weak
- u14 (no_answer): not refused, evidence weak

report: /home/admin/nexa-evidence/nexa-eval-corpus-nomic-embed-20261111-1302.md
        /home/admin/nexa-evidence/nexa-eval-corpus-nomic-embed-20261111-1302.json
baseline written: /home/admin/nexa-eval-0f43e0e/v1/knowledge/benchmark/eval/baselines/corpus-nomic-embed.json (commit it in a reviewed change)
gate FAILED: nothing recorded, deploy.sh will refuse this retrieval change