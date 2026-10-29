"""Identity token and RBAC unit tests (NEXA-ADR-0005)."""
from v1.identity import tokens
from v1.identity.principal import Principal
from v1.identity.rbac import can, permissions_for


def test_token_roundtrip():
    tok = tokens.issue_token("user:t1", ["teacher"], "user", 300)
    claims = tokens.verify_token(tok)
    assert claims["sub"] == "user:t1"
    assert "teacher" in claims["roles"]
    assert claims["typ"] == "user"


def test_invalid_token_rejected():
    try:
        tokens.verify_token("not-a-token")
    except tokens.TokenError:
        return
    raise AssertionError("invalid token should raise TokenError")


def test_rbac_matrix():
    service = Principal("service:x", ("service",))
    student = Principal("user:s", ("student",))
    auditor = Principal("user:a", ("auditor",))
    assert can(service, "model.invoke")
    assert can(service, "knowledge.ingest")
    assert can(student, "model.invoke")
    assert not can(student, "knowledge.ingest")
    assert not can(auditor, "model.invoke")
    assert can(auditor, "knowledge.query")


def test_new_baseline_roles_present():
    perms_admin = permissions_for(["platform_admin"])
    perms_sa = permissions_for(["service_account"])
    assert "admin" in perms_admin
    assert "model.invoke" in perms_sa and "knowledge.ingest" in perms_sa
