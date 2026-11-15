
**Gate: PASS**

| Check | Value | Limit | Result |
|---|---|---|---|
| retrieval.gating.recall@5 | 95.8 | >= 92.5 | pass |
| retrieval.gating.mrr@10 | 0.727 | >= 0.65 | pass |
| retrieval.by_type.exact_fact.recall@5 | 95.0 | >= 90.0 | pass |
| retrieval.by_type.conceptual.recall@5 | 100.0 | >= 92.5 | pass |
| retrieval.by_type.ambiguous.recall@5 | 87.5 | >= 75.0 | pass |
| authority.syllabus_first_rate | 1.0 | >= 0.9 | pass |
| no_answer.correct_refusal_rate | 0.5 | >= 0.4 | pass |
| retrieval.by_type.spelling.recall@5 | 100.0 | >= 80.0 | pass |
| answers.accuracy | 0.875 | >= 0.7 | pass |
| answers.citation_valid_rate | 1.0 | >= 0.95 | pass |
| no_answer.end_to_end_refusal_rate | 1.0 | >= 0.9 | pass |
| no_answer.false_refusal_rate | 0.0 | <= 0.1 | pass |
| latency.p95_ms | 101.1 | <= 450.0 | pass |
| answers.fabricated_citations | 0 | <= 0 | pass |
| answers.ungrounded_rate | 0.0 | <= 0.05 | pass |
| no_answer.curriculum_answer_rate | 0.0 | <= 0.1 | pass |
| retrieval.gating.recall@5 vs baseline 95.8 | 95.8 | >= 93.3 | pass |
| retrieval.gating.recall@1 vs baseline 54.2 | 58.3 | >= 49.2 | pass |
| retrieval.gating.mrr@10 vs baseline 0.706 | 0.727 | >= 0.656 | pass |
| retrieval.gating.ndcg@10 vs baseline 0.774 | 0.79 | >= 0.724 | pass |
| authority.syllabus_first_rate vs baseline 1.0 | 1.0 | >= 0.9 | pass |
| no_answer.correct_refusal_rate vs baseline 0.5 | 0.5 | >= 0.4 | pass |

## Retrieval

| Set | n | recall@1 | recall@5 | recall@10 | mrr@10 | ndcg@10 | precision@5 | source_recall@5 |
|---|---|---|---|---|---|---|---|---|
| gating (approved, EN) | 48 | 58.3 | 95.8 | 97.9 | 0.727 | 0.79 | 0.633 | 93.8 |
| v0 baseline, gating | 48 | 29.2 | 56.2 | 58.3 | 0.391 | 0.438 | 0.583 | 56.2 |
| type=exact_fact | 20 | 55.0 | 95.0 | 100.0 | 0.695 | 0.769 | 0.6 | 95.0 |
| type=conceptual | 20 | 55.0 | 100.0 | 100.0 | 0.725 | 0.794 | 0.64 | 95.0 |
| type=ambiguous | 8 | 75.0 | 87.5 | 87.5 | 0.812 | 0.829 | 0.7 | 87.5 |
| type=spelling | 10 | 50.0 | 100.0 | 100.0 | 0.695 | 0.771 | 0.64 | 100.0 |
| type=multilingual_unapproved | 16 | 37.5 | 81.2 | 87.5 | 0.522 | 0.606 | 0.613 | 81.2 |
| subject=Biology | 29 | 48.3 | 93.1 | 96.6 | 0.662 | 0.738 | 0.669 | 93.1 |
| subject=Chemistry | 12 | 58.3 | 100.0 | 100.0 | 0.75 | 0.813 | 0.65 | 100.0 |
| subject=Physics | 17 | 70.6 | 100.0 | 100.0 | 0.803 | 0.851 | 0.565 | 94.1 |
| grade=11 | 4 | 50.0 | 100.0 | 100.0 | 0.75 | 0.815 | 0.7 | 100.0 |
| grade=12 | 54 | 57.4 | 96.3 | 98.1 | 0.719 | 0.784 | 0.63 | 94.4 |
| term=1 | 12 | 50.0 | 100.0 | 100.0 | 0.7 | 0.775 | 0.583 | 100.0 |
