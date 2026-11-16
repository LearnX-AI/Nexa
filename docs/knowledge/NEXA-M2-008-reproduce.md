# Reproducing the NEXA-M2-008 evaluation

Who this is for: a developer who has not run the evaluation before and wants to reproduce the published
results independently, on their own machine or on the DGX. It goes in order of effort: the fixture suite
needs nothing but Python; the corpus suite needs PostgreSQL with pgvector and Ollama; the live suite needs
a running NEXA Knowledge Service.

Everything below uses only what is in this repository. No secret from a NEXA host is needed: you generate
your own local secret and token.

## 0. What you will compare against

| Suite | Published result | Evidence |
|---|---|---|
| Fixture | 25/25 checks pass (dataset 1.2.0, thresholds 1.4.0) | `evidence/m2-008-eval-fixture-20261116.md` |
| Corpus, v1 | gating recall@5 95.8, recall@1 58.3, MRR@10 0.727, nDCG@10 0.790; answer accuracy 0.875 | `evidence/m2-008-eval-corpus-dgx-20261115.md`, `benchmark/eval/baselines/corpus-nomic-embed.json` |
| Corpus, v0 | gating recall@5 56.2, MRR@10 0.391 | same |
| Live (DGX, DEV) | gating recall@5 83.3, answer accuracy 0.776 | `evidence/m2-008-eval-live-dgx-20261115.md` |
| Live (Pomnsoe school server, offline) | gating recall@5 95.8, answer accuracy 0.879 | `evidence/m2-009-jetson-update-and-offline-20261116.md` |

**What should match exactly, and what may vary.** Retrieval is deterministic for a given corpus, embedding
model and code: recall, MRR, nDCG and precision should match to the published decimal. Ollama versions can
change the vectors slightly (the Jetson's Ollama 0.32.1 and the DGX's 0.21.0 give cosine 0.90 on the same
text), so on a different Ollama version expect retrieval within about ±2 points of recall@5. Answer metrics
use a language model at temperature 0.1–0.2 and vary from run to run: expect accuracy within about ±0.05.
The gate thresholds leave room for this; a result outside these ranges is worth reporting.

## 1. Set up (Ubuntu 22.04 or later, Python 3.10)

```bash
git clone https://github.com/LearnX-AI/Nexa.git && cd Nexa
git checkout <the commit you are reproducing>        # e.g. 9b6c06c for the 2026-11-15 baseline
python3.10 -m venv .venv && . .venv/bin/activate
pip install -r v1/requirements-dev.txt
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng   # OCR for the scanned-syllabus test file
```

## 2. Fixture suite (any machine, about 10 seconds)

```bash
python v1/tools/nexa_eval.py fixture --out eval-out
```

Expected: `Gate: PASS`, 25/25. This is the same command CI runs on every pull request ("RAG evaluation
gate" step in `.github/workflows/ci.yml`).

## 3. Corpus suite (real models; about 15 minutes with a GPU, longer on CPU)

**PostgreSQL with pgvector**, the same image CI uses:

```bash
docker run -d --name nexa-eval-pg -e POSTGRES_PASSWORD=eval-only -p 5432:5432 pgvector/pgvector:0.8.0-pg16
sleep 5; docker exec nexa-eval-pg psql -U postgres -c "CREATE DATABASE nexa_eval"
docker exec nexa-eval-pg psql -U postgres -d nexa_eval -c "CREATE EXTENSION IF NOT EXISTS vector"
export NEXA_KNOWLEDGE_PG_DSN=postgresql://postgres:eval-only@127.0.0.1:5432/nexa_eval
```

**Ollama with the two models** (https://ollama.com/download):

```bash
ollama pull nomic-embed-text
ollama pull llama3.1:8b-instruct-q5_K_M      # only for --answers
```

**The Model Gateway**, with a local secret you generate yourself:

```bash
export NEXA_IDENTITY_SECRET=$(python -c 'import secrets;print(secrets.token_hex(32))')
export NEXA_MODEL_CONFIG=v1/model_gateway/models.example.json
nohup python -m uvicorn v1.model_gateway.main:app --host 127.0.0.1 --port 8100 > gateway.log 2>&1 &
export NEXA_KNOWLEDGE_GATEWAY_TOKEN=$(python -m v1.identity.cli --subject service:eval --roles service --ttl 86400)
curl -s http://127.0.0.1:8100/health
```

**Run it.** The first run builds the M2-002 corpus from the curriculum files at the repository root
(72 approved sources) into `--corpus`; later runs reuse it. The suite indexes the corpus twice (v0 and
v1 pipelines) into a scratch schema that it drops afterwards.

```bash
python v1/tools/nexa_eval.py corpus --corpus ~/nexa-eval-corpus --out eval-out --answers
```

Expected: `Gate: PASS` and the numbers in section 0. The report (`eval-out/nexa-eval-corpus-*.md` and
`.json`) lists every check, every slice and every miss. Do **not** pass `--record-gate` or
`--write-baseline` on your machine: those are for the deploying host.

To check the harness without models (for example on a laptop), `--offline` uses stand-in embedders. It
exercises the whole pipeline but can never pass the gate, and its numbers say nothing about NEXA's quality.

## 4. Live suite (against a running Knowledge Service)

Read-only: it queries `/v1/knowledge/query` and `/v1/knowledge/answer` and changes nothing. It needs the
service's identity secret to issue itself an evaluation token, so it is run on the host itself:

```bash
# DGX (DEV)
(set -a; . <(sudo cat /etc/nexa/identity.env /etc/nexa/knowledge.env); set +a; \
 python v1/tools/nexa_eval.py live --base-url http://127.0.0.1:8103 --out ~/nexa-evidence)

# school server
(set -a; . /etc/nexa-edge/edge.env; set +a; cd /opt/nexa-edge/app && \
 /opt/nexa-edge/venv/bin/python v1/tools/nexa_eval.py live --base-url http://127.0.0.1:8103 --out ~/nexa-evidence)
```

The live result depends on what the service holds (synced LMS content, the school's package), so it is
compared with the live thresholds, not with the corpus numbers.

## 5. Reporting a reproduction

Record in `docs/knowledge/evidence/`: the commit, the date, the machine, the Ollama version, the
`Gate:` line and the gating table from the report, and any difference from section 0 outside the stated
ranges. Exit codes: 0 pass, 1 fail, 2 setup error.

## Troubleshooting

- `NEXA_KNOWLEDGE_GATEWAY_TOKEN not set` or `401` from the gateway: the token must be issued with the same
  `NEXA_IDENTITY_SECRET` the gateway was started with, in the same shell.
- `role "root" does not exist` (PostgreSQL): run as the user that owns the database, not with `sudo`.
- `type "vector" does not exist`: the `CREATE EXTENSION vector` step was skipped.
- Very slow corpus build: OCR of the scanned syllabus takes a few minutes the first time only.
