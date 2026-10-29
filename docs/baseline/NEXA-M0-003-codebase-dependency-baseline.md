# NEXA-M0-003: Repository, Codebase and Dependency Baseline

Status: Verified against the repository on branch nexa-v1-core
Milestone: M0
Evidence base: git remote refs, repo root listing, requirements.txt,
logger_requirements.txt, .python-version, module headers

## 1. Source of truth

There is one source repository: `LearnX-AI/Nexa` on GitHub (the remote reports a
move to the capitalized `Nexa`, which is cosmetic). All active work is in this
repository. No other active source location is known. Code that exists only as
task attachments or on individual machines is flagged in section 7.

## 2. Branches

Verified remote heads: `main`, `dev`, `dev-Bawantha`, `dev-Tharushi`,
`nexa-v1-core`.

| Branch | Purpose | Verification |
|---|---|---|
| main | Integration and deployment line. Current tip merges dev-Tharushi. | VERIFIED tip, purpose inferred |
| dev | Shared development line. | Purpose inferred |
| dev-Bawantha | Developer branch. | Purpose inferred |
| dev-Tharushi | Developer branch, source of the M0 secrets and CORS hardening (commit 07d5258). | VERIFIED commit, owner inferred |
| nexa-v1-core | v1 modernization line. Holds the v1 services and these docs. | VERIFIED |

The live v0 service on the DGX runs from a working copy under /home/admin/Nexa,
not from a checkout of this repository. Reconciling the live copy with the
repository is an open migration item (NEXA-M0-007).

## 3. Runtime entry points

| File | Role | State |
|---|---|---|
| main.py | The live v0 service. `app = FastAPI()` at line 4169, about twenty routes, run as `uvicorn main:app` on 8000. | Production |
| index.html | The single page frontend served by `GET /`. | Production |
| app.py | A second, smaller FastAPI app (247 lines) with its own CORS and HTML responses. Not the production entry. | Stray or experimental |
| ku.py | A minimal FastAPI stub (`app = FastAPI()`), no real routes. | Stray or experimental |
| qwain.py | Qwen image generation using diffusers and torch. Matches the remote image service role on the Ilaibu DGX. | Likely the remote image worker |
| wik.py | Wikipedia and DuckDuckGo (ddgs) web search experiment. | Experimental |
| test.py | transformers and torch import probe. | Throwaway |

Only main.py and index.html are production. The other modules are experimental
or single purpose and must not be assumed to run anywhere until confirmed.
qwain.py should be confirmed as the code deployed on the Ilaibu DGX and, if so,
given its own home in v1 under the Tool and Integration Gateway.

## 4. Package manifests and dependencies

Two manifests exist and they disagree.

- requirements.txt: the main manifest. It is UNPINNED. It lists fastapi,
  uvicorn[standard], python-multipart, pydantic, mysql-connector-python, the
  LangChain stack (langchain, langchain-community, langchain-core,
  langchain-text-splitters, langchain-ollama, langchain-chroma, chromadb),
  pypdf, python-docx, duckduckgo-search, wikipedia, requests, beautifulsoup4,
  markdown-pdf, and the image stack (diffusers, transformers, accelerate, torch,
  Pillow).
- logger_requirements.txt: a second, partially PINNED manifest
  (fastapi==0.116.1, uvicorn==0.35.0, mysql-connector-python==9.3.0,
  pydantic==2.11.7, duckduckgo-search, wikipedia). It overlaps requirements.txt
  and pins different, specific versions.

Python runtime: .python-version pins 3.10.14. The v1 services run on venv310,
consistent with this.

### Dependency risks
- Unpinned main manifest means a rebuild can pull newer, untested versions. v1
  must pin.
- Two manifests that disagree is a provenance risk. There is no single, pinned,
  authoritative dependency set for v0.
- The image stack (diffusers, transformers, torch) is listed in the main
  manifest although main.py never loads a local pipeline (`pipe = None`). Those
  heavy dependencies belong only on the image worker host, not on the main
  service. Carry to the v1 split so the main service does not pull torch.
- requests is used for blocking outbound HTTP. v1 services use async httpx.

## 5. Licence obligations

No LICENSE file is present in the repository and README.md names no licence.
The code's own licence is therefore unstated, which is itself a risk for a
government facing deployment. Separately, the third party dependencies carry
their own licences (for example the LangChain stack, chromadb, torch,
transformers) that must be reviewed for obligations before any redistribution.
Action: add an explicit project licence and run a dependency licence review in
v1.

## 6. Duplicated and tightly coupled logic

- main.py is a single 5599 line module that holds routing, intent
  classification, RAG, model clients, the SymPy engine, image prompt building,
  MySQL access, session state and the frontend serving glue. Everything is
  tightly coupled in one file. This is the central reason for the v1 service
  split (NEXA-M1-001).
- Backup copies are committed at the repo root: index.html.bak, index.html.bak2,
  index.html.broken, index.html.save, index.html.save.1 through .save.4, and
  main.py.bak_dgx, main.py.save, main.py.save.1 through .save.3. These are
  editor and deployment backups in version control. They are a hygiene and
  provenance hazard: it is not obvious which main.py or index.html is
  authoritative from the tree alone.

### Recommended immediate hygiene actions
- Remove the committed .bak, .save, .broken copies from the repository (history
  retains them) and add them to .gitignore.
- Confirm app.py, ku.py, wik.py, test.py are not deployed anywhere, then move
  them out of the production path (an archive folder or deletion with history).
- Confirm qwain.py is the Ilaibu image worker and give it a defined home.

## 7. Code that exists only as attachments or on machines

- The live v0 service runs from /home/admin/Nexa on the DGX, a working copy that
  is not guaranteed identical to any branch. This is production code whose
  authoritative state is a machine, not the repository. Reconcile in
  NEXA-M0-007.
- During the v1 build, service code was moved to the DGX as packaged archives
  (tar) and then committed to nexa-v1-core. The committed copies on
  nexa-v1-core are now the source of truth for the v1 services; no v1 service
  should remain only as an archive.
- qwain.py and the Ilaibu image worker: confirm the running code on
  100.92.95.83 matches qwain.py in the repository, or capture it if it diverges.

## 8. Recommended v1 repository structure

Adopt the structure defined in NEXA-M1-001: one repository, one package per
service under v1/, shared code under v1/shared and v1/observability, deployment
manifests under v1/deploy, and documentation under docs/. Pin every dependency
per service in that service's requirements.txt. Keep the heavy image stack off
the main service. Add a project licence. The full tree, ownership map, import
rules and migration mapping are in NEXA-M1-001.

## 9. Verification status

| Item | Status |
|---|---|
| Single source repository | VERIFIED |
| Branch list | VERIFIED (ls-remote); branch purposes inferred |
| Production entry points main.py and index.html | VERIFIED |
| Stray and experimental modules | VERIFIED present; runtime role inferred |
| Two disagreeing manifests, main one unpinned | VERIFIED |
| Python 3.10.14 | VERIFIED (.python-version) |
| No project licence | VERIFIED (no LICENSE, README silent) |
| Committed backup copies at root | VERIFIED |
| Live service runs from a machine working copy, not a checkout | VERIFIED (deployment path), reconciliation open |
