# Recorded baselines (NEXA-M2-008)

One file per embedding prefix scheme, `corpus-<scheme>.json`, written by

    python v1/tools/nexa_eval.py corpus --corpus DIR --out OUT --write-baseline

on the DGX with the real models, then committed in a reviewed change. The corpus
suite compares a candidate with the baseline for the same embedding fingerprint
and fails if a metric drops by more than `max_drop_vs_baseline` in
`../thresholds.v*.json`. Each file also holds the v0 baseline measured in the
same run.

Replace a baseline only when a change is meant to move the numbers (a new
model, a deliberate trade-off), and say why in the pull request.
