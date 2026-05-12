import pytest

from app.config import GuardrailsPolicy, SafetyRule
from app.guardrails.safety import GuardrailViolation, SafetyFilter


@pytest.fixture
def safety() -> SafetyFilter:
    return SafetyFilter(
        GuardrailsPolicy(
            enabled=True,
            rules=[
                SafetyRule(
                    name="no-drop",
                    tool="execute_sql",
                    forbidden_substrings=["DROP TABLE"],
                ),
                SafetyRule(
                    name="bounded-limit",
                    tool="*",
                    numeric_ranges={"limit": {"min": 1, "max": 100}},
                ),
                SafetyRule(
                    name="no-secret-regex",
                    tool="*",
                    forbidden_regex=[r"sk-[A-Za-z0-9]{10,}"],
                ),
            ],
        )
    )


def test_blocks_forbidden_substring(safety):
    with pytest.raises(GuardrailViolation) as exc:
        safety.check("execute_sql", {"query": "DROP TABLE users"})
    assert exc.value.rule == "no-drop"


def test_allows_safe_sql(safety):
    safety.check("execute_sql", {"query": "SELECT 1"})


def test_numeric_range_enforced(safety):
    with pytest.raises(GuardrailViolation):
        safety.check("any", {"limit": 9999})


def test_regex_blocks(safety):
    with pytest.raises(GuardrailViolation):
        safety.check("any", {"text": "key=sk-abcdefghijklmnop"})
