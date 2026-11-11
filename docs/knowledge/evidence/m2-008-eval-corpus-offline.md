# NEXA-M2-008 evaluation: corpus suite

Dataset 1.0.0, thresholds 1.0.0. Run 2026-11-11T10:50:23+00:00, commit `9dd3521eafbd`, gate key `2d58e7adbb69-2f1e1c309f21`.

Index nexa-embed-default|nomic-embed-text:v1.5|offline-standin|768|nomic-embed, built in 8.7 s (72 sources, 1811 chunks). Store pgvector.

> **Offline stand-in embedders.** This checks the harness only; the numbers say nothing about the real models and can never pass the gate.

**Gate: FAIL** (offline stand-in embedders: harness check only, never a release gate)

| Check | Value | Limit | Result |
|---|---|---|---|
| retrieval.gating.recall@5 | 72.9 | >= 85.0 | **FAIL** |
| retrieval.gating.mrr@10 | 0.527 | >= 0.6 | **FAIL** |
| retrieval.by_type.exact_fact.recall@5 | 80.0 | >= 85.0 | **FAIL** |
| retrieval.by_type.conceptual.recall@5 | 70.0 | >= 85.0 | **FAIL** |
| retrieval.by_type.ambiguous.recall@5 | 62.5 | >= 75.0 | **FAIL** |
| authority.syllabus_first_rate | 1.0 | >= 0.8 | pass |
| no_answer.correct_refusal_rate | 1.0 | >= 0.75 | pass |
| no_answer.false_refusal_rate | 0.271 | <= 0.1 | **FAIL** |
| latency.p95_ms | 32.6 | <= 450.0 | pass |

## Retrieval

| Set | n | recall@1 | recall@5 | recall@10 | mrr@10 | ndcg@10 | precision@5 |
|---|---|---|---|---|---|---|---|
| gating (approved, EN) | 48 | 37.5 | 72.9 | 87.5 | 0.527 | 0.61 | 0.533 |
| v0 baseline, gating | 48 | 14.6 | 25.0 | 33.3 | 0.198 | 0.23 | 0.242 |
| type=exact_fact | 20 | 55.0 | 80.0 | 95.0 | 0.676 | 0.741 | 0.56 |
| type=conceptual | 20 | 25.0 | 70.0 | 80.0 | 0.432 | 0.521 | 0.54 |
| type=ambiguous | 8 | 25.0 | 62.5 | 87.5 | 0.391 | 0.501 | 0.45 |
| type=multilingual_unapproved | 16 | 31.2 | 50.0 | 56.2 | 0.417 | 0.453 | 0.45 |
| subject=Biology | 27 | 37.0 | 74.1 | 88.9 | 0.512 | 0.602 | 0.519 |
| subject=Chemistry | 9 | 22.2 | 66.7 | 77.8 | 0.437 | 0.52 | 0.533 |
| subject=Physics | 12 | 50.0 | 75.0 | 91.7 | 0.626 | 0.695 | 0.567 |
| grade=11 | 2 | 50.0 | 100.0 | 100.0 | 0.6 | 0.693 | 0.6 |
| grade=12 | 46 | 37.0 | 71.7 | 87.0 | 0.523 | 0.606 | 0.53 |
| term=1 | 9 | 33.3 | 88.9 | 88.9 | 0.593 | 0.669 | 0.644 |
| term=2 | 7 | 57.1 | 85.7 | 100.0 | 0.665 | 0.743 | 0.571 |
| term=3 | 8 | 25.0 | 50.0 | 75.0 | 0.374 | 0.461 | 0.425 |
| term=None | 24 | 37.5 | 70.8 | 87.5 | 0.512 | 0.598 | 0.517 |
| language=en | 48 | 37.5 | 72.9 | 87.5 | 0.527 | 0.61 | 0.533 |
| format=docx | 23 | 39.1 | 73.9 | 87.0 | 0.529 | 0.61 | 0.539 |
| format=pdf | 24 | 37.5 | 75.0 | 87.5 | 0.541 | 0.621 | 0.55 |
| format=pptx | 1 | 0.0 | 0.0 | 100.0 | 0.125 | 0.315 | 0.0 |
| source_type=lms_content | 23 | 39.1 | 73.9 | 87.0 | 0.529 | 0.61 | 0.539 |
| source_type=official_syllabus | 9 | 66.7 | 88.9 | 100.0 | 0.792 | 0.842 | 0.556 |
| source_type=student_guide | 12 | 16.7 | 58.3 | 75.0 | 0.347 | 0.443 | 0.567 |
| source_type=teacher_guide | 3 | 33.3 | 100.0 | 100.0 | 0.567 | 0.673 | 0.467 |
| source_type=teacher_material | 1 | 0.0 | 0.0 | 100.0 | 0.125 | 0.315 | 0.0 |

v1 minus v0 (gating set): recall@1 +22.9, recall@5 +47.9, recall@10 +54.2, mrr@10 +0.329, ndcg@10 +0.38, precision@5 +0.291.

## Authority, refusal, latency

- Syllabus-defined questions with an official syllabus passage first: 1.0 (n=6)
- Unanswerable questions refused (evidence none): 1.0 (n=16); answerable questions wrongly refused: 0.271 (n=48)
- Retrieval latency p50 23.2 ms, p95 32.6 ms

## Misses

- q05 (conceptual): expected passage not in the top 10; top was STEMPhysics-Term1-StudentGuide-Gr12.pdf
- q09 (conceptual): expected passage not in the top 10; top was STEMChem-Term3-Syllabus-Gr12.pdf
- q14 (conceptual): expected passage not in the top 10; top was STEMChem-Term3-StudentGuide-Gr12.pdf
- q32 (exact_fact): expected passage not in the top 10; top was s18_cm8921_12.4_Bioenergetics_5_The Moses Experiment (Burning Bush).docx
- q33 (conceptual): expected passage not in the top 10; top was STEMBio-Term2-Syllabus-Gr12.pdf
- q08-tpi (multilingual): expected passage not in the top 10; top was STEMPhysics-Term3-StudentGuide-Gr12.pdf
- q10-tpi (multilingual): expected passage not in the top 10; top was STEMBio-Term2-TeacherGuide-Gr11.pdf
- q14-tpi (multilingual): expected passage not in the top 10; top was STEMPhysics-Term3-StudentGuide-Gr12.pdf
- q30-tpi (multilingual): expected passage not in the top 10; top was s16_cm8915_12.4_Bioenergetics_3_Biogas Production.docx
- q35-tpi (multilingual): expected passage not in the top 10; top was STEMBio-Term2-Syllabus-Gr12.pdf
- q37-tpi (multilingual): expected passage not in the top 10; top was STEMBio-Term2-TeacherGuide-Gr12.pdf
- q12-tpi (multilingual): expected passage not in the top 10; top was s23_cm8936_12.6_Health and Medical_1_Homeostasis (Concept  Mechanisms).docx
- a04 (ambiguous): expected passage not in the top 10; top was s14_cm8909_12.4_Bioenergetics_1_Photosynthesis_Lesson Notes.docx
