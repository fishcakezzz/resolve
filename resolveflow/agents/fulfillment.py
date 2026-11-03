"""Fulfillment Investigator: inspect order and delivery evidence only."""

from .contracts import AgentState
from .investigator import investigate


def run(state: AgentState) -> dict:
    return {"fulfillment_result": investigate(state, "fulfillment_investigator", {"MISSING_ITEM", "LATE_DELIVERY"})}
