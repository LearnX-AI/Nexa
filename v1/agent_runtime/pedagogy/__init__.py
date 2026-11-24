"""NEXA Teaching Intelligence Policy (NEXA-M4-001).

One versioned policy (contract nexa.pedagogy/1.0) for the instructional
behaviour of every education agent. Agent manifests reference it by version
and profile; the Agent Runtime composes the profile into the system message
of every call, so no agent carries its own copy of the teaching rules.
"""
from .contract import (  # noqa: F401
    POLICY_ID, Composed, PedagogyError, Policy, compose, get, json_schema, load_all, screen, versions,
)
