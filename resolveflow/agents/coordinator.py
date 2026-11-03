"""Case Coordinator: turn confirmed issues into scoped investigation tasks."""

from uuid import uuid4

from ..decomposition import draft_case
from .contracts import AgentState


FULFILLMENT_FIELDS = {"order_owner", "order_total_cents", "missing_item_confirmed", "logistics_status", "order_items", "fulfillments", "packages"}
PAYMENT_FIELDS = {"payment_status", "refunded_cents", "duplicate_charge_confirmed", "payment_transactions", "refund_transactions"}


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
                "case_id": state["case_id"],
                "role": role,
                "issue_keys": [issue["key"] for issue in relevant],
                "objective": "核实履约事实" if role == "fulfillment_investigator" else "核实资金事实",
                "order_ref": state["order_id"],
                "allowed_fields": sorted(fields),
                "evidence_version": state.get("evidence_version"),
                "max_steps": 8,
            })
    return {"tasks": tasks}


def review(state: AgentState) -> dict:
    findings = [finding for key in ("fulfillment_result", "payment_result")
                for finding in state[key].get("findings", [])]
    missing = sorted({field for finding in findings for field in finding["missing_fields"]})
    conflicts = sorted({field for finding in findings for field in finding.get("conflicts", [])})
    return {"review": {
        "status": "BLOCKED" if conflicts else "NEEDS_INFO" if missing else "READY_FOR_PLAN",
        "missing_fields": missing,
        "conflicts": conflicts,
        "next_action": "TAKEOVER_CASE" if conflicts else "COLLECT_EVIDENCE" if missing else "PLAN",
    }}
