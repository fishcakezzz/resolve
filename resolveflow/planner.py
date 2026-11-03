"""Candidate action generation used by the Resolution Planner agent."""

from .llm import LLMConfig, propose_with_model


def plan(issue_type: str, requested_amount_cents: int, sku: str | None, facts: dict) -> dict:
    if LLMConfig.from_env() is not None:
        return propose_with_model(
            issue_type,
            requested_amount_cents,
            sku,
            facts,
        ).model_dump()
    if issue_type == "MISSING_ITEM" and sku:
        return {"action_type": "RESHIP", "amount_cents": 0, "reason": "缺件：建议补发指定商品"}
    if issue_type == "DUPLICATE_CHARGE":
        return {
            "action_type": "REFUND",
            "amount_cents": requested_amount_cents,
            "reason": "重复扣款：建议退回多收款项",
        }
    if issue_type == "LATE_DELIVERY":
        return {
            "action_type": "REFUND",
            "amount_cents": requested_amount_cents,
            "reason": "物流延误：建议按售后政策补偿",
        }
    return {
        "action_type": "REFUND",
        "amount_cents": requested_amount_cents,
        "reason": "缺件：建议按商品金额退款",
    }
