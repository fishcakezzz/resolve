from .models import Case, Proposal, Subcase


POLICY_VERSION = "v1"
REVIEW_THRESHOLD_CENTS = 5000


def evaluate(case: Case, subcase: Subcase, proposal: Proposal, facts: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    required = {"order_owner", "payment_status", "order_total_cents", "refunded_cents"}
    if subcase.issue_type == "DUPLICATE_CHARGE":
        required.add("duplicate_charge_confirmed")
    if subcase.issue_type == "LATE_DELIVERY":
        required.add("logistics_status")
    if subcase.issue_type == "MISSING_ITEM":
        required.add("missing_item_confirmed")

    missing = sorted(required - facts.keys())
    if missing:
        return "NEEDS_INFO", [f"缺少关键证据: {', '.join(missing)}"]
    if facts["order_owner"] != case.customer_id:
        reasons.append("订单不属于当前用户")
    if facts["payment_status"] != "PAID":
        reasons.append("支付状态不是 PAID")
    if subcase.issue_type == "DUPLICATE_CHARGE" and facts["duplicate_charge_confirmed"] is not True:
        reasons.append("重复扣款未经证实")
    if subcase.issue_type == "MISSING_ITEM" and facts["missing_item_confirmed"] is not True:
        reasons.append("缺件未经证实")
    if subcase.issue_type == "LATE_DELIVERY" and facts["logistics_status"] != "LATE":
        reasons.append("物流延误未经证实")
    if proposal.action_type == "REFUND":
        if proposal.amount_cents <= 0:
            reasons.append("退款金额必须大于零")
        elif proposal.amount_cents > subcase.requested_amount_cents:
            reasons.append("退款金额超过该问题申报金额")
        elif proposal.amount_cents > int(facts["order_total_cents"]) - int(facts["refunded_cents"]):
            reasons.append("退款金额超过可退余额")
    if proposal.action_type == "RESHIP":
        if subcase.issue_type != "MISSING_ITEM":
            reasons.append("此问题类型不允许补发")
        if not proposal.sku:
            reasons.append("补发缺少 SKU")
    if reasons:
        return "DENY", reasons
    if proposal.action_type == "REFUND" and proposal.amount_cents >= REVIEW_THRESHOLD_CENTS:
        return "REVIEW", ["退款金额达到人工审批阈值"]
    return "ALLOW", ["通过策略校验"]
