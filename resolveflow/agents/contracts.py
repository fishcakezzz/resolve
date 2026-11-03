"""Shared, bounded task contracts for the four analysis roles."""

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    case_id: str
    order_id: str
    evidence_version: int
    issues: list[dict[str, Any]]
    facts: dict[str, Any]
    evidence_refs: dict[str, dict[str, Any]]
    tasks: list[dict[str, Any]]
    fulfillment_result: dict[str, Any]
    payment_result: dict[str, Any]
    review: dict[str, Any]
    plan: dict[str, Any]
    proposals: dict[str, dict[str, Any]]


class EvidenceSnapshotTool:
    """Read only the fields authorized for one investigator."""

    def __init__(self, allowed_fields: set[str], facts: dict, refs: dict):
        self.allowed_fields = allowed_fields
        self.facts = facts
        self.refs = refs

    def read(self, field: str) -> dict:
        if field not in self.allowed_fields:
            raise ValueError(f"field {field} is outside this agent's tool scope")
        if field not in self.facts:
            return {"field": field, "status": "MISSING"}
        return {"field": field, "status": "AVAILABLE", "value": self.facts[field], "evidence_ref": self.refs.get(field)}
