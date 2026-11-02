"""LangGraph analysis graph. Business authorization remains in policy.py."""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .llm import LLMConfig, propose_with_model


class AnalysisState(TypedDict, total=False):
    issue_type: str
    requested_amount_cents: int
    sku: str | None
    facts: dict[str, Any]
    action_type: str
    amount_cents: int
    reason: str


def propose_action(state: AnalysisState) -> AnalysisState:
    if LLMConfig.from_env() is not None:
        return propose_with_model(
            state["issue_type"],
            state["requested_amount_cents"],
            state.get("sku"),
            state.get("facts", {}),
        ).model_dump()
    issue = state["issue_type"]
    if issue == "MISSING_ITEM" and state.get("sku"):
        return {"action_type": "RESHIP", "amount_cents": 0, "reason": "缺件：建议补发指定商品"}
    if issue == "DUPLICATE_CHARGE":
        return {
            "action_type": "REFUND",
            "amount_cents": state["requested_amount_cents"],
            "reason": "重复扣款：建议退回多收款项",
        }
    if issue == "LATE_DELIVERY":
        return {
            "action_type": "REFUND",
            "amount_cents": state["requested_amount_cents"],
            "reason": "物流延误：建议按售后政策补偿",
        }
    return {
        "action_type": "REFUND",
        "amount_cents": state["requested_amount_cents"],
        "reason": "缺件：建议按商品金额退款",
    }


builder = StateGraph(AnalysisState)
builder.add_node("propose_action", propose_action)
builder.add_edge(START, "propose_action")
builder.add_edge("propose_action", END)
analysis_graph = builder.compile()


def plan(issue_type: str, requested_amount_cents: int, sku: str | None, facts: dict) -> dict:
    return analysis_graph.invoke(
        {
            "issue_type": issue_type,
            "requested_amount_cents": requested_amount_cents,
            "sku": sku,
            "facts": facts,
        }
    )
