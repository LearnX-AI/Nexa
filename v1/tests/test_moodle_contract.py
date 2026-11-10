"""NEXA-M2-007: the Moodle plugin and the Knowledge Service agree on the contract.

Runs the plugin's own mapping code (PHP, outside Moodle) to build sync
batches, then validates them with NEXA's contract model and pushes them
through the real sync endpoint. Skipped when PHP is not installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from v1.identity import tokens
from v1.knowledge import embeddings
from v1.knowledge import main as km
from v1.knowledge.indexing import IndexManager
from v1.knowledge.ingestion.pipeline import Pipeline
from v1.knowledge.lms_sync import SyncBatch
from v1.knowledge.store import InMemoryStore

from .knowledge_fixtures import FakeGateway

PLUGIN = os.path.join(os.path.dirname(__file__), "..", "integrations", "moodle", "local_nexa_integration")
PHP = shutil.which("php")
pytestmark = pytest.mark.skipif(not PHP, reason="php not installed")


def _batches():
    r = subprocess.run([PHP, os.path.join(PLUGIN, "tests", "contract_check.php")], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr  # the plugin's own rule checks
    return json.loads(r.stdout)


def test_plugin_batches_validate_and_apply(monkeypatch):
    monkeypatch.setattr(embeddings, "_gateway", FakeGateway())
    pipe = Pipeline()
    monkeypatch.setattr(km, "PIPE", pipe)
    monkeypatch.setattr(km, "INDEX", IndexManager(InMemoryStore(), pipe.registry))
    tok = tokens.issue_token("lms:edunex-ncd", ["lms_sync"], "service", 300,
                             extra={"lms_site": "edunex-ncd", "school_id": "pg-ncd-0042"})
    with TestClient(km.app) as c:
        for batch in _batches():
            SyncBatch(**batch)  # NEXA's own contract model accepts it
            r = c.post("/v1/knowledge/lms/events?wait=true", headers={"Authorization": f"Bearer {tok}"}, json=batch)
            assert r.status_code == 200, r.text
            states = {x["item_key"].rsplit(":", 1)[1]: x.get("state") for x in r.json()["results"]}
            assert states == {"cm345": "applied", "cm346": "applied", "cm300": "applied"}
    hidden = pipe.registry.get_lms_item("moodle:edunex-ncd:cm346")
    src = pipe.registry.get_source(hidden["version_id"])
    assert src["metadata"]["access_classification"] == "teacher_only"  # hidden in Moodle
    page = pipe.registry.get_source(pipe.registry.get_lms_item("moodle:edunex-ncd:cm345")["version_id"])
    md = page["metadata"]
    assert md["subject"] == "Biology" and md["grade_levels"] == [12] and md["scope"] == "school"
    assert md["jurisdiction"]["school_id"] == "pg-ncd-0042" and md["approval_status"] == "approved"
