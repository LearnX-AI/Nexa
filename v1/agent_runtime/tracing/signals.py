"""Operational signals of agent execution (NEXA-M3-008).

Two kinds, neither carrying content or a user's identity:

  metrics  Prometheus counters on GET /metrics of the Agent Runtime, labels
           of low cardinality only (agent, role, model, tool, status, rule,
           workflow), never a user, a prompt or an id:
             nexa_agent_calls_total{agent,role,status}
             nexa_agent_call_ms_sum / _count{agent,role}
             nexa_model_tokens_total{model,kind=prompt|completion}
             nexa_tool_calls_total{tool,status}
             nexa_tool_call_ms_sum / _count{tool}
             nexa_policy_decisions_total{action,rule,decision}   (policy/pdp.py)
             nexa_workflow_runs_total{workflow,status}
  spans    one JSON log line per agent or tool call (event "span"), with the
           trace id, run id, span id (the request's message id), parent span
           (the step), latency and outcome; journald keeps them with every
           other service's lines under the same trace id
"""
from __future__ import annotations

from typing import Any, Optional

from v1.observability import log_event
from v1.observability.core import metrics


def _status(ok: bool, code: Optional[str]) -> str:
    return "ok" if ok else (code or "error")


def agent_call(run: Any, step: str, agent: str, role: str, span_id: str, ms: int, *, ok: bool,
               code: Optional[str] = None, model: Optional[str] = None, prompt_tokens: Optional[int] = None,
               completion_tokens: Optional[int] = None) -> None:
    status = _status(ok, code)
    metrics.inc("nexa_agent_calls_total", agent=agent, role=role, status=status)
    metrics.observe("nexa_agent_call_ms", ms, agent=agent, role=role)
    if model:
        if prompt_tokens:
            metrics.inc("nexa_model_tokens_total", prompt_tokens, model=model, kind="prompt")
        if completion_tokens:
            metrics.inc("nexa_model_tokens_total", completion_tokens, model=model, kind="completion")
    log_event(event="span", span="agent.call", trace_id=run.trace_id, run_id=run.run_id, span_id=span_id,
              parent=f"step:{step}", workflow=run.workflow_id, agent=agent, role=role, status=status, latency_ms=ms,
              model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def tool_call(run: Any, step: str, tool: str, agent: str, span_id: str, ms: int, *, ok: bool,
              code: Optional[str] = None) -> None:
    status = _status(ok, code)
    metrics.inc("nexa_tool_calls_total", tool=tool, status=status)
    metrics.observe("nexa_tool_call_ms", ms, tool=tool)
    log_event(event="span", span="tool.call", trace_id=run.trace_id, run_id=run.run_id, span_id=span_id,
              parent=f"step:{step}", workflow=run.workflow_id, agent=agent, tool=tool, status=status, latency_ms=ms)


def run_finished(run: Any, status: str) -> None:
    metrics.inc("nexa_workflow_runs_total", workflow=run.workflow_id, status=status)
    log_event(event="span", span="workflow.run", trace_id=run.trace_id, run_id=run.run_id, span_id=f"run:{run.run_id}",
              workflow=run.workflow_id, status=status, steps=run.counters.get("steps"),
              agent_calls=run.counters.get("agent_calls"), running_s=round(run.counters.get("running_s", 0.0), 2))
