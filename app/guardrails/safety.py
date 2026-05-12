"""Safety filter / guardrails for tool arguments.

Rules are declarative (see `app.config.SafetyRule`):
  * `forbidden_substrings` — block calls whose string arguments contain banned
    substrings (case-insensitive). Useful for things like SQL keywords.
  * `forbidden_regex` — block on regex pattern matches.
  * `numeric_ranges` — enforce per-argument numeric bounds.

The filter is intentionally fail-closed: when a rule matches, the call is
blocked and a metric is emitted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import GuardrailsPolicy, SafetyRule
from app.observability.logging import get_logger
from app.observability.metrics import GUARDRAIL_BLOCKS_TOTAL

log = get_logger("guardrails")


class GuardrailViolation(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"guardrail '{rule}' violated: {detail}")
        self.rule = rule
        self.detail = detail


@dataclass
class _CompiledRule:
    rule: SafetyRule
    regexes: list[re.Pattern[str]]
    forbidden_lower: list[str]


class SafetyFilter:
    def __init__(self, policy: GuardrailsPolicy) -> None:
        self.policy = policy
        self._compiled: list[_CompiledRule] = [
            _CompiledRule(
                rule=r,
                regexes=[re.compile(p, re.IGNORECASE) for p in r.forbidden_regex],
                forbidden_lower=[s.lower() for s in r.forbidden_substrings],
            )
            for r in policy.rules
        ]

    def _iter_strings(self, obj: Any) -> list[str]:
        out: list[str] = []
        if isinstance(obj, str):
            out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                out.extend(self._iter_strings(v))
        elif isinstance(obj, list):
            for v in obj:
                out.extend(self._iter_strings(v))
        return out

    def _check_rule(self, c: _CompiledRule, tool: str, args: dict[str, Any]) -> None:
        if c.rule.tool != "*" and c.rule.tool != tool:
            return
        strings = self._iter_strings(args)
        for s in strings:
            lower = s.lower()
            for bad in c.forbidden_lower:
                if bad in lower:
                    GUARDRAIL_BLOCKS_TOTAL.labels(rule=c.rule.name, tool=tool).inc()
                    raise GuardrailViolation(
                        c.rule.name, f"forbidden substring '{bad}' in argument"
                    )
            for pattern in c.regexes:
                if pattern.search(s):
                    GUARDRAIL_BLOCKS_TOTAL.labels(rule=c.rule.name, tool=tool).inc()
                    raise GuardrailViolation(
                        c.rule.name, f"forbidden pattern '{pattern.pattern}' matched"
                    )
        for arg_name, bounds in c.rule.numeric_ranges.items():
            if arg_name not in args:
                continue
            val = args[arg_name]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                continue
            lo = bounds.get("min")
            hi = bounds.get("max")
            if lo is not None and val < lo:
                GUARDRAIL_BLOCKS_TOTAL.labels(rule=c.rule.name, tool=tool).inc()
                raise GuardrailViolation(
                    c.rule.name, f"{arg_name}={val} below min {lo}"
                )
            if hi is not None and val > hi:
                GUARDRAIL_BLOCKS_TOTAL.labels(rule=c.rule.name, tool=tool).inc()
                raise GuardrailViolation(
                    c.rule.name, f"{arg_name}={val} above max {hi}"
                )

    def check(self, tool: str, arguments: dict[str, Any]) -> None:
        if not self.policy.enabled:
            return
        for c in self._compiled:
            self._check_rule(c, tool, arguments)
