"""Observability tests (NEXA-M1-007): redaction, spans, metrics, trace ids."""
import json

import pytest

from v1.observability import log_event, metrics, redact, span


def test_redact_masks_sensitive_keys_recursively():
    out = redact({"authorization": "Bearer abc", "ok": 1,
                  "nested": {"messages": [{"content": "secret"}], "model": "m"}})
    assert out["authorization"] == "[redacted]"
    assert out["nested"]["messages"] == "[redacted]"
    assert out["nested"]["model"] == "m" and out["ok"] == 1


def test_log_event_is_one_json_line_and_redacted(capsys):
    log_event(event="x", email="a@b.pg", prompt="hello", model="m")
    line = capsys.readouterr().out.strip()
    data = json.loads(line)
    assert data["email"] == "[redacted]" and data["prompt"] == "[redacted]"
    assert "a@b.pg" not in line and data["model"] == "m"


def test_span_logs_latency_status_and_trace(capsys):
    with span("rag.query", "trace-1", k=3):
        pass
    with pytest.raises(ValueError):
        with span("tool.fail", "trace-1"):
            raise ValueError("x")
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert lines[0]["span"] == "rag.query" and lines[0]["status"] == "ok"
    assert lines[0]["trace_id"] == "trace-1" and "latency_ms" in lines[0]
    assert lines[1]["status"] == "error"
    assert 'nexa_span_ms_count{span="tool.fail",status="error"} 1' in metrics.render()


def test_metrics_render_prometheus_text():
    metrics.inc("nexa_test_total", 2, a="b")
    metrics.observe("nexa_test_ms", 5.0, a="b")
    out = metrics.render()
    assert 'nexa_test_total{a="b"} 2' in out
    assert 'nexa_test_ms_count{a="b"} 1' in out and 'nexa_test_ms_sum{a="b"} 5' in out
