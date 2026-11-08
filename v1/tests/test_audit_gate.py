"""The dependency audit gate's exception rules (no network)."""
import datetime as dt
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "audit_gate", pathlib.Path(__file__).resolve().parents[1] / "tools" / "audit_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

DEPS = [{"name": "Pillow", "version": "12.3.0",
         "vulns": [{"id": "PYSEC-1", "aliases": ["CVE-1"], "fix_versions": ["13.0.0"]}]},
        {"name": "pypdf", "version": "6.21.4", "vulns": []}]
TODAY = dt.date(2026, 11, 2)


def exc(**over):
    e = {"package": "pillow", "version": "12.3.0", "expires": "2027-01-31", "ids": ["CVE-1"]}
    e.update(over)
    return [e]


def test_covered_by_alias_passes():
    assert gate.evaluate(DEPS, exc(), TODAY) == []


def test_uncovered_expired_and_stale_fail():
    assert "not covered" in gate.evaluate(DEPS, [], TODAY)[0]
    assert any("expired" in p for p in gate.evaluate(DEPS, exc(expires="2026-10-01"), TODAY))
    assert any("stale" in p for p in gate.evaluate(DEPS, exc(version="11.0.0"), TODAY))
    assert any("not covered" in p for p in gate.evaluate(DEPS, exc(ids=["CVE-OTHER"]), TODAY))


def test_committed_exceptions_are_well_formed():
    import json
    data = json.loads((pathlib.Path(gate.EXCEPTIONS)).read_text())
    for e in data["exceptions"]:
        assert {"package", "version", "expires", "ids", "reason", "mitigation", "exit", "approved_by"} <= set(e)
        assert dt.date.fromisoformat(e["expires"]) - dt.date(2026, 11, 2) <= dt.timedelta(days=120)
