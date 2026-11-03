"""Case Coordinator: turn confirmed issues into scoped investigation tasks."""

from uuid import uuid4

from ..decomposition import draft_case
from .contracts import AgentState


FULFILLMENT_FIELDS = {"order_owner", "order_total_cents", "missing_item_confirmed", "logistics_status"}
PAYMENT_FIELDS = {"payment_status", "refunded_cents", "duplicate_charge_confirmed"}


def draft(request_text: str) -> dict:
    result = draft_case(request_text)
    result["agent"] = "case_coordinator"
    return result


def dispatch(state: AgentState) -> dict:
    issues = state["issues"]
    tasks = []
    for role, types, fields in (
        ("fulfillment_investigator", {"MISSING_ITEM", "LATE_DELIVERY"}, FULFILLMENT_FIELDS),
        ("payment_investigator", {"DUPLICATE_CHARGE"}, PAYMENT_FIELDS),
    ):
        relevant = [issue for issue in issues if issue["issue_type"] in types]
        if relevant:
            tasks.append({
                "task_id": str(uuid4()),
                "role": role,
                "issue_keys": [issue["key"] for issue in relevant],
                "allowed_fields": sorted(fields),
                "evidence_version": state.get("evidence_version"),
                "max_steps": 8,
            })
    return {"tasks": tasks}
