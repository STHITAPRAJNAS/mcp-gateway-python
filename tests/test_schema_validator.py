"""Tests for JSON Schema validation of tool arguments."""
import pytest

from app.guardrails.schema_validator import SchemaValidationError, ToolSchemaValidator


SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["query"],
}


def test_valid_arguments_pass():
    v = ToolSchemaValidator(enabled=True)
    v.validate("my_tool", {"query": "hello", "limit": 10}, SCHEMA)


def test_missing_required_field_raises():
    v = ToolSchemaValidator(enabled=True)
    with pytest.raises(SchemaValidationError) as exc_info:
        v.validate("my_tool", {"limit": 5}, SCHEMA)
    errors = exc_info.value.field_errors
    assert any("query" in str(e) for e in errors)


def test_wrong_type_raises():
    v = ToolSchemaValidator(enabled=True)
    with pytest.raises(SchemaValidationError):
        v.validate("my_tool", {"query": 123}, SCHEMA)


def test_out_of_range_raises():
    v = ToolSchemaValidator(enabled=True)
    with pytest.raises(SchemaValidationError):
        v.validate("my_tool", {"query": "x", "limit": 9999}, SCHEMA)


def test_disabled_validator_skips_check():
    v = ToolSchemaValidator(enabled=False)
    # Should not raise even with invalid args.
    v.validate("my_tool", {}, SCHEMA)


def test_missing_schema_not_strict():
    v = ToolSchemaValidator(enabled=True, strict_on_missing_schema=False)
    # No schema provided → skip validation.
    v.validate("my_tool", {"anything": True}, None)


def test_missing_schema_strict_raises():
    v = ToolSchemaValidator(enabled=True, strict_on_missing_schema=True)
    with pytest.raises(SchemaValidationError):
        v.validate("my_tool", {"anything": True}, None)


def test_empty_schema_allows_anything():
    v = ToolSchemaValidator(enabled=True)
    # {} means any object is valid.
    v.validate("my_tool", {"x": 1, "y": "foo"}, {})


def test_error_has_field_info():
    v = ToolSchemaValidator(enabled=True)
    with pytest.raises(SchemaValidationError) as exc_info:
        v.validate("my_tool", {"limit": "notanint"}, SCHEMA)
    assert isinstance(exc_info.value.field_errors, list)
    assert len(exc_info.value.field_errors) > 0
