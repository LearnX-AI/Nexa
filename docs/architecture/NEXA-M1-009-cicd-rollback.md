# NEXA-M1-009: CI/CD, Quality Gates and Rollback

Status: CI gates, pip-audit, deploy and rollback scripts implemented; first live run on a QA host pending
Milestone: M1

## 1. Pipeline

The pipeline is defined in `.github/workflows/ci.yml` and runs on every push and
pull request to `nexa-v1-core`.

Gates, in order, build fails if any fails:
1. Lint: `ruff check --select F v1` (pyflakes subset, honors noqa).
2. Compile: `python -m compileall -q v1`.
3. Service-boundary import check: `python v1/tools/check_imports.py` (fails on a
   model SDK imported outside the backends, or a cross-service internal import).
4. Tests: `python -m pytest v1/tests` (identity, registries, config, endpoint
   auth).
5. Config validation gate: proves `preflight` detects a missing required
   variable.
6. Dependency and security scan: `pip-audit` against the pinned per-service
   requirements.
7. Build or version artifact: writes and uploads `nexa-version.txt` and a
   `nexa-v1-<sha>.tar.gz` of `v1/` with its sha256.
8. Deploy to DEV (`deploy-dev`), then QA (`promote-qa`) behind the `qa`
   environment approval. Both run only on push, only when the repository
   variable `NEXA_DEPLOY_ENABLED` is `true`, on self-hosted runners labelled
   `nexa-dev` and `nexa-qa`.

## 2. Dependency and security scanning

- Per-service requirements are pinned (`v1/*/requirements.txt`), with dev tools
  in `v1/requirements-dev.txt`. `pip-audit` runs as a CI gate and fails the build
  on a known vulnerability. Enable Dependabot version updates in GitHub to get
  upgrade pull requests.

## 3. Promotion

- DEV: every green build on `nexa-v1-core` is deployable to DEV by pull and
  restart.
- QA: the `promote-qa` job uses the `qa` GitHub environment. Set Required
  reviewers on it (Settings > Environments > qa) to make promotion an approval
  step. The approver checks the gates passed and the version artifact.
- UAT and PROD: later, with their own approvals.

## 4. Deployment

Deployment is `v1/deploy/deploy.sh <sha>`: it checks out the commit (detached),
installs the pinned requirements, restarts the five services, and waits for
every health URL. If a service is unhealthy it rolls back to the previous commit
automatically and exits non-zero. A changed systemd unit under `v1/deploy/` must
still be copied to `/etc/systemd/system` and followed by `systemctl
daemon-reload`.

## 5. Rollback

Automatic: `deploy.sh` rolls back on a failed health check. Manual:
`v1/deploy/rollback.sh [sha]` returns to the previous successful deploy (read
from `.nexa_deploy_history`) or to the given commit, through the same health
gate. Because services run from a git checkout and hold no build state, rollback
is a checkout plus a restart. Nothing is destroyed. The manual steps below remain
valid.

1. Identify the last good commit from the version artifact or `git log`.
2. On the host:
   ```
   cd /home/admin/Nexa/Nexa
   git checkout <last-good-sha>
   sudo systemctl restart nexa-api-gateway nexa-agent-runtime \
        nexa-model-gateway nexa-identity nexa-knowledge
   ```
3. Confirm health: `curl -s http://127.0.0.1:8099/ready` and each `/health`.
4. If a configuration change caused the failure, restore the previous env file
   from the operations record and restart; env files are not in git.
5. Return to the branch tip once the fix is ready
   (`git checkout nexa-v1-core`).

The deploy and rollback scripts were tested against a throwaway git repository
(healthy deploy, unhealthy deploy with automatic rollback). A rollback on a real
QA host has not yet been exercised; doing so is part of the M1 exit evidence
(NEXA-M1-010).

## 6. Acceptance check

- One change moves from repository to DEV with recorded checks: met (CI gates
  plus the version artifact).
- QA promotion with an approval gate: implemented; needs the QA host, runner
  and `qa` environment reviewers.
- Tested rollback path: script tested locally; live exercise on QA outstanding.
