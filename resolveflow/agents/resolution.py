"""Resolution Planner: propose actions from shared, read-only findings."""

from ..planner import plan
from .contracts import AgentState


def run(state: AgentState) -> dict:
    proposals = {}
    for issue in state["issues"]:
        proposals[issue["key"]] = plan(
            issue["issue_type"], issue["requested_amount_cents"], issue.get("sku"), state["facts"]
        )
    return {"proposals": proposals}
