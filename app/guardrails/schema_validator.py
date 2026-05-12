"""Tool argument validation against the tool's own JSON Schema (inputSchema).

Runs after guardrails and before PII redaction. Catches structural errors
(wrong types, missing required fields, extra properties) before the call
reaches the upstream — avoiding wasted network round-trips and giving the
LLM a precise, field-level error to self-correct against.

Config:
  schema_validation:
    enabled: true
    # When true, missing or empty inputSchema is treated as "any args OK".
    # When false, missing schema causes a warning but does not block.
    strict_on_missing_schema: false
"""
from __future__ import annotations

from typing import Any

from jsonschema import Draft7Validator, ValidationError as JsonSchemaError
from jsonschema.exceptions import SchemaError

from app.observability.logging import get_logger
from app.observability.metrics import GUARDRAIL_BLOCKS_TOTAL

log = get_logger("schema_validator")


class SchemaValidationError(Exception):
    def __init__(self, message: str, field_errors: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.field_errors = field_errors


class ToolSchemaValidator:
    def __init__(self, *, enabled: bool = True, strict_on_missing_schema: bool = False) -> None:
        self.enabled = enabled
        self.strict_on_missing_schema = strict_on_missing_schema

    def validate(
        self, tool_name: str, arguments: dict[str, Any], input_schema: dict[str, Any] | None
    ) -> None:
        """Raise SchemaValidationError if arguments violate the tool's inputSchema."""
        if not self.enabled:
            return

        if not input_schema:
            if self.strict_on_missing_schema:
                raise SchemaValidationError(
                    f"tool '{tool_name}' has no inputSchema and strict validation is enabled",
                    [{"field": "$", "error": "missing inputSchema"}],
                )
            return

        # Guard against a malformed schema crashing the gateway.
        try:
            validator = Draft7Validator(input_schema)
        except SchemaError as exc:
            log.warning(
                "schema_validator.bad_schema",
                tool=tool_name,
                error=str(exc),
            )
            return

        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
        if not errors:
            return

        field_errors = [
            {
                "field": ".".join(str(p) for p in err.absolute_path) or "$",
                "error": err.message,
            }
            for err in errors
        ]
        GUARDRAIL_BLOCKS_TOTAL.labels(rule="schema_validation", tool=tool_name).inc()
        log.info(
            "schema_validator.blocked",
            tool=tool_name,
            error_count=len(field_errors),
            fields=[e["field"] for e in field_errors],
        )
        raise SchemaValidationError(
            f"tool '{tool_name}' argument validation failed ({len(field_errors)} error(s))",
            field_errors=field_errors,
        )
