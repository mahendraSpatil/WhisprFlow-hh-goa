"""Models for the incident memory: what is stored, and what is handed back."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models.agents import Contract, Severity


class Incident(Contract):
    """One remembered incident, as the History view lists it."""

    id: int
    signature: str
    run_id: str
    source: str = Field(description="The repo the run analyzed, with credentials redacted.")
    created_at: str = Field(description="UTC, ISO 8601.")
    kind: str = Field(description="The exception type, or the rule id for findings that are not exceptions.")
    pattern: str
    owasp: str | None = None
    category: str
    severity: Severity
    rule_id: str
    title: str
    file: str
    line: int
    symbol: str | None = None
    root_cause: str | None = None
    patch_status: str = Field(description='"valid", "invalid", "skipped", or "none" when no patch was attempted.')
    patch_diff: str | None = None
    patch_design: str | None = None
    regression_passed: bool | None = Field(default=None, description="None when there was no patch to verify.")
    regression_reasons: list[str] = []
    occurrences: int = Field(default=1, description="Incidents sharing this signature, across all runs.")


class PastFix(Contract):
    """An accepted fix for a similar incident, offered to Claude as an example."""

    incident_id: int
    match: Literal["exact", "similar"]
    seen_at: str
    kind: str
    owasp: str | None
    root_cause: str
    diff: str
    design: str
