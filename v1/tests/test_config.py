"""Config preflight unit tests (NEXA-M1-006)."""
import os

import pytest

from v1.shared.config import preflight, missing, ConfigError


def test_preflight_passes_when_present():
    os.environ["NEXA_TEST_REQUIRED"] = "value"
    preflight("test", ["NEXA_TEST_REQUIRED"])


def test_preflight_raises_when_missing():
    os.environ.pop("NEXA_TEST_ABSENT", None)
    with pytest.raises(ConfigError):
        preflight("test", ["NEXA_TEST_ABSENT"])


def test_missing_lists_blanks():
    os.environ["NEXA_TEST_BLANK"] = "   "
    assert "NEXA_TEST_BLANK" in missing(["NEXA_TEST_BLANK"])


@pytest.mark.parametrize("module", [
    "v1.api_gateway.main",
    "v1.agent_runtime.main",
    "v1.knowledge.main",
    "v1.model_gateway.main",
])
def test_services_refuse_to_start_without_identity_secret(module, monkeypatch):
    """NEXA-M1-006 acceptance: a missing required secret stops the service."""
    import importlib
    from fastapi.testclient import TestClient

    app = importlib.import_module(module).app
    monkeypatch.delenv("NEXA_IDENTITY_SECRET", raising=False)
    with pytest.raises(ConfigError):
        with TestClient(app):
            pass
