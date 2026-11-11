# NEXA live check: DGX, 2026-11-11 10:31 UTC

Read-only end-to-end check of the live v1 services (deploy `9dd3521`), run with `v1/tools/nexa_live_check.py`
after the full EduNex Moodle sync (plugin 2.0.1, 473 lessons, course categories auto-approved, test school
`edunex-core-test`). Full JSON on the DGX: `~/nexa-evidence/nexa-live-check-20261111-1031.json`.

**OVERALL: PASS (20/20).**

```
[PASS] health: api-gateway (:8099)  {"version": "0.2.0"}
[PASS] health: model-gateway (:8100)  {"version": "0.2.0"}
[PASS] health: identity (:8101)  {"version": "0.1.0"}
[PASS] health: agent-runtime (:8102)  {"version": "0.1.0"}
[PASS] health: knowledge (:8103)  {"version": "0.3.0"}
[PASS] health: knowledge index active  {"index": "idx_20261110062959_4a9e3c21", "embedding": "nexa-embed-default|nomic-embed-text|ollama|768|nomic-embed", "retrieval": {"mode": "auto", "rerank": "auto", "profile": "cent
[PASS] lms sync: all received events applied  {"events": {"applied": 956, "stale": 2}}
[PASS] lms sync: no item waiting or failing  {"items": {"active": 473, "withdrawn": 3}}
[PASS] lms sync: lessons served  {"active": 473}
[PASS] retrieval: LMS lesson on the topic in the top 5 (10/10)  {"recall_at_5": "100%", "questions": [{"question": "What are the steps of the scientific method?", "rank": 1, "top_title": "Sciencetific Methods and Skills (Lesson Notes, Scienceti
[PASS] access: admin sees LMS content  {"results": 10}
[PASS] access: students outside 'edunex-core-test' never see its school-scoped content  {"leaked": 0, "school_scoped_items_in_admin_view": 6}
[PASS] access: a student of 'edunex-core-test' does see it  {"results": 6}
[PASS] access: students never see teacher-only content  {"leaked": 0}
[PASS] access: teachers can retrieve  {"results": 10}
[PASS] answers: Explain how DNA replication works.  {"status": "grounded", "confidence": "high", "citations": [["S1", "DNA Replication_PPT", 1], ["S2", "Modern Molecular Genetics_Teacher_Note", 24], ["S3", "DNA Replication (Lesson N
[PASS] answers: What are the steps of the scientific method?  {"status": "grounded", "confidence": "high", "citations": [["S1", "Sciencetific Methods and Skills (Lesson Notes, Sci", 3], ["S2", "Sciencetific_Methods and Skills_Teacher Note", 3
[PASS] answers: What happens during transcription of DNA into RNA?  {"status": "grounded", "confidence": "high", "citations": [["S1", "Transcription and Translation (Lesson Notes, Genet", 4], ["S2", "Transcription &amp; Translation_Teature Note", 4
[PASS] answers: off-curriculum question gets the 'not found' fallback  {"status": "no_evidence", "answer": "I could not find this in the approved curriculum materials available to you, so I will not guess. Try rephrasing the que"}
[PASS] latency: retrieval p95 under 1 s  {"p50_ms": 92, "p95_ms": 103}

OVERALL: PASS (20/20)
```

## Detail (from the report)

| answers | What are the steps of the scientific method? | **PASS** | {"status": "grounded", "confidence": "high", "citations": [["S1", "Sciencetific Methods and Skills (Lesson Notes, Sci", 3], ["S2", "Sciencetific_Methods and Ski |
| answers | What happens during transcription of DNA into RNA? | **PASS** | {"status": "grounded", "confidence": "high", "citations": [["S1", "Transcription and Translation (Lesson Notes, Genet", 4], ["S2", "Transcription &amp; Translat |

## Retrieval questions (LMS content, top 5)

| Question | Rank | Top lesson | Score |
|---|---|---|---|
| What are the steps of the scientific method? | 1 | Sciencetific Methods and Skills (Lesson Notes, Sciencetific Methods and Skills) | 0.849 |
| How do scientists write a testable hypothesis? | 1 | Sciencetific_Methods and Skills_Lecture_PPT | 0.768 |
| What is the difference between genes and chromosomes? | 1 | Genes Chromosomes (Lesson Notes, Genetics and Molecular Biology) | 0.827 |
| What enzyme unwinds the DNA double helix during replication? | 1 | DNA Replication (Lesson Notes, Genetics and Molecular Biology) | 0.84 |
| What happens during transcription of DNA into RNA? | 1 | Transcription and Translation (Lesson Notes, Genetics and Molecular Biology) | 0.713 |
| How is mRNA translated into a protein at the ribosome? | 1 | Transcription and Translation (Lesson Notes, Genetics and Molecular Biology) | 0.789 |
| What are independent, dependent and controlled variables? | 1 | Scientific_Inquiry_Process_LessonNote | 0.752 |
| How do you record and analyse data in a scientific investigation? | 1 | Scientific_Inquiry_Process | 0.748 |

## Grounded answers

**Q: Explain how DNA replication works.** (grounded, 13040 ms)

> DNA replication is the process by which one double-stranded DNA molecule produces two identical copies, so genetic information passes accurately from one cell generation to the next [S1]. This occurs in the S (synthesis) phase of the cell cycle, before mitosis or meiosis [S1].

The process begins wh

Citations: [('S1', 'DNA Replication_PPT', 1), ('S2', 'Modern Molecular Genetics_Teacher_Note', 24), ('S3', 'DNA Replication (Lesson Notes, Genetics and Molecu', 4), ('S4', 'DNA Replication (Lesson Notes, Genetics and Molecu', 7), ('S5', 'DNA Replication_Teacher Note', 7)]

**Q: What are the steps of the scientific method?** (grounded, 3713 ms)

> The steps of the scientific method include:

* Observation [S1]
* Question [S1][S2]
* Hypothesis [S1][S2][S3]
* Prediction [S1][S2]
* Experiment [S1][S2]
* Data collection and analysis [S1][S2]
* Conclusion [S1][S2]

Note that the scientific method is often represented as a cycle, not a straight lin

Citations: [('S1', 'Sciencetific Methods and Skills (Lesson Notes, Sci', 3), ('S2', 'Sciencetific_Methods and Skills_Teacher Note', 3), ('S3', 'Sciencetific Methods and Skills (Lesson Notes, Sci', 3)]

**Q: What happens during transcription of DNA into RNA?** (grounded, 3309 ms)

> During transcription, the enzyme RNA polymerase binds to the start of a gene, unwinds the DNA, and reads one strand (the template strand). It builds a complementary strand of mRNA, pairing bases as A–U, T–A, C–G, G–C - note that RNA uses uracil (U) in place of thymine. The finished mRNA leaves the n

Citations: [('S1', 'Transcription and Translation (Lesson Notes, Genet', 4), ('S2', 'Transcription &amp; Translation_Teature Note', 4), ('S4', 'Transcription and Translation (teacher deck)', 5)]

**Q: off-curriculum question gets the 'not found' fallback** (no_evidence, - ms)

> I could not find this in the approved curriculum materials available to you, so I will not guess. Try rephrasing the que

Citations: None
