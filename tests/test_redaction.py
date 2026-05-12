from app.config import RedactionPolicy
from app.middleware.redaction import Redactor


def test_redact_email_in_nested_structure():
    redactor = Redactor(RedactionPolicy(enabled=True))
    payload = {"user": {"contact": "alice@example.com"}, "notes": ["call 555-123-4567"]}
    cleaned, changed = redactor.redact(payload, direction="response")
    assert changed
    assert "alice@example.com" not in str(cleaned)


def test_redaction_disabled_no_op():
    redactor = Redactor(RedactionPolicy(enabled=False))
    payload = {"email": "bob@example.com"}
    cleaned, changed = redactor.redact(payload, direction="request")
    assert not changed
    assert cleaned == payload


def test_redaction_direction_toggle():
    policy = RedactionPolicy(enabled=True, redact_requests=False, redact_responses=True)
    redactor = Redactor(policy)
    payload = {"email": "bob@example.com"}
    _, req_changed = redactor.redact(payload, direction="request")
    _, resp_changed = redactor.redact(payload, direction="response")
    assert req_changed is False
    assert resp_changed is True
