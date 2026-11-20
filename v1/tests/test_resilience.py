"""NEXA-M3-009: multi-agent evaluation, failure handling and resilience.
Critical workflows pass the agreed thresholds and failures degrade safely."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from v1.agent_runtime.evaluation import runner, sandbox
from v1.agent_runtime.evaluation.scenarios import CATEGORIES, SCENARIOS


@pytest.fixture(scope="module")
def report():
    return runner.run()


def test_every_category_is_covered():
    assert {s.category for s in SCENARIOS} == set(CATEGORIES)
    assert len(SCENARIOS) >= 60


def test_failure_handling_scenarios_all_pass(report):
    failed = [f"{r['id']}: {r['detail']}" for r in report["results"]
              if r["category"] != "routing" and not r["passed"]]
    assert not failed, failed


def test_no_safety_invariant_is_ever_violated(report):
    assert report["safety_violations"] == []


def test_routing_meets_its_threshold(report):
    c = report["categories"]["routing"]
    assert c["meets"], [r["detail"] for r in report["results"] if r["category"] == "routing" and not r["passed"]]


def test_the_thresholds_are_met_and_the_report_renders(report):
    assert report["meets_thresholds"]
    md = runner.markdown(report)
    assert "**MET**" in md and "| routing |" in md


def test_invariants_catch_an_unsafe_run():
    """The checker itself: a failed run that released output is flagged."""
    from v1.agent_runtime import registry
    with sandbox.World(sandbox.Model(always="unavailable")) as w:
        r = w.start("nexa-wf-lesson-explainer", sandbox.TEACHER, {"topic": "x y", "grade": "11"})
        registry.REGISTRY.store.query("UPDATE workflow_runs SET output = 'leaked' WHERE run_id = ?", (r["run_id"],))
    assert any("V3 no unsafe output" in v for v in sandbox.LAST_VIOLATIONS)


def test_live_mode_against_the_real_apps(monkeypatch, capsys, tmp_path):
    """The live evaluation's logic, run against the API Gateway and Agent
    Runtime in process with a scripted model."""
    from v1.agent_runtime.main import app as runtime_app
    from v1.api_gateway import main as api
    from v1.tools import resilience_eval
    from v1.agent_runtime.tracing import api as tracing_api
    from v1.tool_gateway.main import app as tool_app
    with sandbox.World(sandbox.Model(coordinator=[{"action": "explain", "task": "Explain."},
                                                  {"action": "worked_example", "task": "One."}]),
                       sandbox.Knowledge(status=lambda q: "grounded")):
        monkeypatch.setattr(api, "_transport", httpx.ASGITransport(app=runtime_app))
        monkeypatch.setattr(tracing_api, "TRANSPORT", httpx.ASGITransport(app=tool_app))
        real_sleep = time.sleep
        with TestClient(api.app) as gw:
            monkeypatch.setattr(resilience_eval.time, "sleep", lambda s: real_sleep(0.01))
            monkeypatch.setattr(httpx, "Client", lambda base_url, timeout: gw)
            code = resilience_eval.live("http://127.0.0.1:8099", 2, "eval-school", str(tmp_path))
    out = "\n".join(x for x in capsys.readouterr().out.splitlines() if not x.startswith("{"))
    assert code == 0, out
    rep = json.loads(next(tmp_path.glob("nexa-resilience-live-*.json")).read_text())
    assert rep["metrics"]["coordinator_valid_choice"] == 1.0 and rep["metrics"]["safety_violations"] == 0
