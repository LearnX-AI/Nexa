# NEXA-M1-009: CI/CD, Quality Gates and Rollback

Status: CI implemented; promotion and rollback are documented procedures
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
6. Build or version artifact: writes and uploads `nexa-version.txt` with the git
   short SHA and build time.

## 2. Dependency and security scanning

- Dependency and vulnerability scanning (for example `pip-audit` or GitHub
  Dependabot) is the one gate not yet wired, because it needs the pinned
  per-service requirements and a scanning token or action to be enabled. Add
  `pip-audit` as a gate once dependencies are pinned (NEXA-M0-003 action).

## 3. Promotion

- DEV: every green build on `nexa-v1-core` is deployable to DEV by pull and
  restart.
- QA: promotion to QA is a manual approval step once QA hosts exist
  (NEXA-M1-008). The approver checks the gates passed and the version artifact.
- UAT and PROD: later, with their own approvals.

## 4. Deployment

Deployment is pull plus systemd (no build step for Python services):

```
cd /home/admin/Nexa/Nexa
git fetch origin && git checkout nexa-v1-core && git pull
# update any changed unit under /etc/systemd/system from v1/deploy, then:
sudo systemctl restart nexa-<service>.service
```

## 5. Rollback

Because services run from a git checkout and hold no build state, rollback is a
checkout of the last good commit and a restart. Nothing is destroyed.

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

A rollback has not yet been exercised in a live pipeline; doing so is part of the
M1 exit evidence (NEXA-M1-010).

## 6. Acceptance check

- One change moves from repository to DEV with recorded checks: met (CI gates
  plus the version artifact).
- QA promotion with an approval gate: defined, pending QA hosts.
- Tested rollback path: procedure defined; live exercise outstanding.
