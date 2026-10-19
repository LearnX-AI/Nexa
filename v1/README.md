# NEXA v1 Core

This folder holds the NEXA v1 platform services. It is additive: NEXA v0 keeps
running from its own files and is not modified by anything here.

Milestone: M1, Core Platform Foundation (API Gateway, Model Gateway, Identity
and policy, Agent Runtime skeleton, Observability).

Gate rule (NEXA-DEV-001 section 15): do not build future-phase work here. M2
storage and RAG, M3 agents and later only start after their prior gate passes.

## Services

- `model_gateway/`  The NEXA Model Gateway (ADR-0001). One OpenAI-compatible
  entry to every approved model. First service of M1.

More services (identity, agent runtime skeleton, observability) are added in
order as M1 proceeds.

## Governing documents

- NEXA-ARCH-001 Master Architecture and Engineering Baseline
- NEXA-MIG-001 v0 to v1 Migration Plan
- NEXA-GOV-001 Governance, Safety, Data and Trust Framework
- NEXA-ADR-0001 Model Gateway and Inference Abstraction
