# NEXA-M2-007 LMS sync demonstration: live DGX service

Run 2026-11-11 00:56 UTC against the live Knowledge Service (http://127.0.0.1:8103, deploy 451ac6f,
pgvector, nomic-embed-text v1.5 via the Model Gateway), demo site nexa-demo, school nexa-demo-school,
run id ed44a6. Everything the demo created was withdrawn at the end. Full transcript on the DGX:
~/nexa-evidence/m2-007-lms-sync-demo.json.

| Step | Result | Detail |
|---|---|---|
| Approved LMS page becomes retrievable for its school | PASS | applied; source lms:nexa-demo:pged44a6, lms://nexa-demo/course/demo-bio12/item/pged44a6?rev=1 |
| Event for a file not yet uploaded fails and is visible | PASS | "content cb9d9e47... has not been uploaded" |
| After upload by hash, retry succeeds and the file is retrievable | PASS | 36,673 bytes stored; applied on attempt 2 |
| Other schools and users without a school cannot see school content | PASS | |
| Changed revision supersedes the old one | PASS | |
| Pending revision does not replace the approved one | PASS | |
| Hidden in the LMS: teachers see it, students do not | PASS | |
| Stale event ignored, resent event is a duplicate | PASS | stale, duplicate |
| Reconcile withdraws an item deleted while offline | PASS | not_in_lms [rced44a6], withdrawn [rced44a6] |
| Deleted item is gone from retrieval; its events stay in the log | PASS | |

**demo PASSED: 10 steps.**

Live verification after the same deploy (nexa_m2_verify.py): OVERALL PASS. Index 1,811 chunks, 0 missing,
0 stale, self-recall@5 0.94. Hybrid retrieval, recall@5 all content EN 97.5 / TPI 81.2 (vector before:
87.5 / 62.5), as a student EN 90.0 / TPI 75.0, 0 of 20 teacher-only leaks, p95 106 ms.
