"""Agent registry (NEXA-ADR-0003).

An agent is a named configuration over a logical gateway model: a system prompt
and the permission a caller must hold to run it. This is the M1 skeleton, so the
set is small and static. Tool access and per-agent data scopes arrive with the
Tool Gateway (NEXA-ADR-0006) in a later phase and are not added here.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    model: str
    system_prompt: str
    required_permission: str = "model.invoke"


AGENTS: dict[str, AgentSpec] = {
    "nexa-tutor": AgentSpec(
        name="nexa-tutor",
        description="General student tutor for PNG senior school subjects.",
        model="nexa-chat-default",
        system_prompt=(
            "You are NEXA, a patient tutor for Papua New Guinea senior school "
            "students. Explain clearly, step by step, in plain English. Keep "
            "answers accurate and age appropriate."
        ),
    ),
    "nexa-precise": AgentSpec(
        name="nexa-precise",
        description="Low temperature assistant for factual and mathematical answers.",
        model="nexa-chat-precise",
        system_prompt=(
            "You are NEXA in precise mode. Give concise, correct answers. Show "
            "your working for any calculation."
        ),
    ),
}


def get_agent(name: str) -> Optional[AgentSpec]:
    return AGENTS.get(name)
