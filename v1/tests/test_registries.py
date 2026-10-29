"""Model and agent registry unit tests (NEXA-ADR-0001, ADR-0003)."""
from v1.model_gateway import registry as mreg
from v1.agent_runtime import registry as areg


def test_model_resolve_and_runtimes():
    chat = mreg.resolve("nexa-chat-default")
    general = mreg.resolve("nexa-general")
    assert chat is not None and chat.runtime == "ollama"
    assert general is not None and general.runtime == "openai"
    # Two different adapters serve two logical names: the ADR-0001 switch.
    assert chat.runtime != general.runtime


def test_model_unknown():
    assert mreg.resolve("does-not-exist") is None


def test_agent_fields():
    tutor = areg.get_agent("nexa-tutor")
    assert tutor is not None
    assert tutor.version
    assert tutor.deprecated is False
    assert "chat" in tutor.capabilities
    assert tutor.required_permission == "model.invoke"
