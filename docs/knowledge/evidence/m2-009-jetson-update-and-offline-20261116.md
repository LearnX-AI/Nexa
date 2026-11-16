# NEXA-M2-009 on the Pomnsoe school server: update, rollback and offline evaluation

Host: Pomnsoe Jetson Orin NX 16 GB (no network path to core NEXA; packages moved as files via a PC).
Code: main at 6324138. Ollama 0.32.1 with the lean settings in
[m2-009-load-jetson-20261116.md](m2-009-load-jetson-20261116.md).

## Packages built on core NEXA (DGX), 2026-11-16 10:53 UTC

```
full  v2: 71 sources, 1801 chunks, 16.0 MB      nexa-edge-pomnsoe-v2-full.tar.gz
delta v2: 71 sources, 1801 chunks, 5.9 MB, 12 changed/new sources, 0 withdrawn
                                                nexa-edge-pomnsoe-v2-delta-from-v1.tar.gz
excluded: {"another school's content": 4576}
```

The 4,576 excluded chunks are LMS items scoped to another school (`edunex-core-test`): the package
holds only national and Pomnsoe content. Chunks per subject, read from the installed packages:

| Package | Biology | Chemistry | Physics | Total |
|---|---|---|---|---|
| v1 | 602 | 0 | 533 | 1,135 |
| v2 | 602 | 666 | 533 | 1,801 |

## Update, rollback, update again (school server)

| Step | Command | Result |
|---|---|---|
| A | `status` | serving v1 (59 sources, 1,135 chunks) |
| B | `install pomnsoe-v2-delta.tar.gz` | installed v2 (delta from v1): 71 sources, 1,801 chunks; 666 new chunks re-embedded with the school server's model (probe cosine 0.9006 against core) |
| C | `rollback` | now serving v1; answers report package v1 |
| D | `install pomnsoe-v2-full.tar.gz` | installed v2 (full): 1,801 chunks re-embedded; serving v2 |

Every answer reports the package version it used (`x_nexa.package_version`), and it followed each step.
After the delta (B) and after the full package (D), the same question gave the same result, and the top
passages for a Chemistry question came from the new Chemistry Student Guides. "What is a covalent bond?"
is a general answer on both v1 and v2: the Grade 12 Chemistry guides discuss bonding but do not define a
covalent bond, so NEXA correctly does not present the curriculum as covering it.

Signature, school, version order, file and content checks ran on every install (`v1/edge/sync.py`).
Withdrawal by delta is covered by `test_delta_update_applies_changes_and_withdrawals`; no withdrawal was
needed on the Jetson.

## M2-008 live suite against the school server (offline), 2026-11-16 11:17 UTC

`nexa_eval.py live --base-url http://127.0.0.1:8103`, dataset 1.2.0, thresholds 1.4.0, package v2.
**Gate: PASS.**

| Check | Jetson (offline) | DGX live, 2026-11-15 | Limit |
|---|---|---|---|
| retrieval.gating.recall@5 | 95.8 | 83.3 | >= 80.0 |
| retrieval.gating.recall@10 | 100.0 | 93.8 | >= 90.0 |
| answers.accuracy | 0.879 | 0.776 | >= 0.7 |
| no_answer.end_to_end_refusal_rate | 1.0 | | >= 0.9 |
| no_answer.false_refusal_rate | 0.017 | | <= 0.1 |
| answers.fabricated_citations | 0 | 0 | 0 |
| latency.p95_ms (retrieval) | 378.2 | | <= 1000 |

By subject, recall@5: Biology 96.6 (n=29), Chemistry 91.7 (n=12), Physics 100.0 (n=17). Grounded answers:
54 grounded, 4 general, cited an expected source 0.914, key-term coverage 0.894, no ungrounded answers.
Unanswerable questions: all 16 got no curriculum answer (end-to-end refusal 1.0); 5 of them (u02, u03, u10,
u12, u13) had weak rather than no evidence, which is reported, not gated.

The school server scores higher than core NEXA's live run. The likely reason is that the package holds only
this school's approved content: the other school's 4,576 LMS chunks, including near-copies of
the guides that compete for the top places on the core, are not in it. This is an observation, not a claim that the school server is the
better retriever.

Two copies of the suite ran at the same time by accident; this affects only timing, not results
(each question is answered independently, and retrieval latency stayed within the limit).

## Observations for follow-up

- Correction (2026-11-16): an earlier version of this note said grade was missing from the package. It is
  not: grade is stored as `grade_levels` (for example `"12"`), and the check that printed `None` read a
  field called `grade`, which does not exist.
- The general-answer prompt forced PNG comparisons and invented people and Tok Pisin; fixed in the
  closing PR (explain first, PNG examples only where they fit).
