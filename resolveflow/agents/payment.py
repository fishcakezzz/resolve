"""Payment Investigator: inspect payment and refund evidence only."""

from .contracts import AgentState
from .investigator import investigate


def run(state: AgentState) -> dict:
    return {"payment_result": investigate(state, "payment_investigator", {"DUPLICATE_CHARGE"})}
