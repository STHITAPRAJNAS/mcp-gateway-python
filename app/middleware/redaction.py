"""PII redaction middleware using Microsoft Presidio.

Presidio is optional at import time — if unavailable the redactor degrades to
a regex-based fallback so tests and minimal deployments still work.
"""
from __future__ import annotations

import re
from typing import Any

from app.config import RedactionPolicy
from app.observability.logging import get_logger
from app.observability.metrics import REDACTIONS_TOTAL

log = get_logger("redaction")

# Conservative regex fallbacks used when presidio is unavailable or for entities
# it does not natively cover at low latency.
_FALLBACK_PATTERNS: dict[str, re.Pattern[str]] = {
    "EMAIL_ADDRESS": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "PHONE_NUMBER": re.compile(r"\+?\d[\d \-().]{7,}\d"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "IP_ADDRESS": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


class Redactor:
    def __init__(self, policy: RedactionPolicy) -> None:
        self.policy = policy
        self._analyzer = None
        if policy.enabled:
            try:
                from presidio_analyzer import AnalyzerEngine

                self._analyzer = AnalyzerEngine()
                log.info("redaction.presidio.loaded")
            except Exception as exc:  # noqa: BLE001
                log.warning("redaction.presidio.unavailable", error=str(exc))
                self._analyzer = None

    def _replace(self, entity: str) -> str:
        return self.policy.replacement.format(entity=entity)

    def _redact_string(self, text: str, direction: str) -> tuple[str, bool]:
        if not text or not self.policy.enabled:
            return text, False
        changed = False
        # Presidio pass
        if self._analyzer is not None:
            try:
                results = self._analyzer.analyze(
                    text=text,
                    entities=self.policy.entities,
                    language="en",
                    score_threshold=self.policy.score_threshold,
                )
                # Apply replacements right-to-left to preserve indices.
                for r in sorted(results, key=lambda r: r.start, reverse=True):
                    text = text[: r.start] + self._replace(r.entity_type) + text[r.end :]
                    REDACTIONS_TOTAL.labels(direction=direction, entity=r.entity_type).inc()
                    changed = True
            except Exception as exc:  # noqa: BLE001
                log.warning("redaction.presidio.error", error=str(exc))
        # Regex fallback for entities not covered or when presidio absent.
        for entity in self.policy.entities:
            pattern = _FALLBACK_PATTERNS.get(entity)
            if pattern is None:
                continue
            new_text, n = pattern.subn(self._replace(entity), text)
            if n:
                REDACTIONS_TOTAL.labels(direction=direction, entity=entity).inc(n)
                text = new_text
                changed = True
        return text, changed

    def redact(self, obj: Any, *, direction: str) -> tuple[Any, bool]:
        """Recursively redact strings in arbitrary JSON-like structures."""
        if not self.policy.enabled:
            return obj, False
        if direction == "request" and not self.policy.redact_requests:
            return obj, False
        if direction == "response" and not self.policy.redact_responses:
            return obj, False
        return self._walk(obj, direction)

    def _walk(self, obj: Any, direction: str) -> tuple[Any, bool]:
        if isinstance(obj, str):
            return self._redact_string(obj, direction)
        if isinstance(obj, dict):
            changed = False
            out = {}
            for k, v in obj.items():
                new_v, c = self._walk(v, direction)
                out[k] = new_v
                changed = changed or c
            return out, changed
        if isinstance(obj, list):
            changed = False
            out_l = []
            for v in obj:
                new_v, c = self._walk(v, direction)
                out_l.append(new_v)
                changed = changed or c
            return out_l, changed
        return obj, False
