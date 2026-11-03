"""Resolution Planner: propose actions from shared, read-only findings."""

from ..planner import plan
from .contracts import AgentState


def run(state: AgentState) -> dict:
    proposals = {}
    findings = {finding["issue_key"]: finding for key in ("fulfillment_result", "payment_result")
                for finding in state[key].get("findings", [])}
    blockers = []
    for issue in state["issues"]:
        finding = findings[issue["key"]]
        if finding["conclusion"] == "UNKNOWN":
            blockers.append(f"{issue['key']}: 调查结论未知")
        elif finding["conclusion"] == "REFUTED":
            blockers.append(f"{issue['key']}: 诉求未获证实")
        proposals[issue["key"]] = plan(
            issue["issue_type"], issue["requested_amount_cents"], issue.get("sku"), state["facts"]
        )
    return {"proposals": proposals, "plan": {
        "status": "BLOCKED" if blockers else "PROPOSED",
        "actions": [{"issue_key": key, "finding": findings[key]["conclusion"], **proposal}
                    for key, proposal in proposals.items()],
        "blockers": blockers,
        "evidence_version": state["evidence_version"],
    }}
