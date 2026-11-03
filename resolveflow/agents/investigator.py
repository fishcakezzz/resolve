"""Bounded evidence inspection shared by the two specialized investigators."""

from ..policy import required_evidence
from ..llm import LLMConfig, select_investigation_field
from .contracts import AgentState, EvidenceSnapshotTool


def _domain_conflicts(role: str, observed: dict) -> list[str]:
    def value(field):
        row = observed.get(field, {})
        return row.get("value") if row.get("status") == "AVAILABLE" else None

    conflicts = []
    if role == "payment_investigator":
        transactions = value("payment_transactions")
        if isinstance(transactions, list):
            succeeded = {row.get("transaction_id") for row in transactions
                         if isinstance(row, dict) and row.get("status") == "SUCCEEDED" and row.get("transaction_id")}
            confirmed = value("duplicate_charge_confirmed")
            if confirmed is not None and (len(succeeded) >= 2) != confirmed:
                conflicts.append("duplicate_charge_confirmed")
        refunds = value("refund_transactions")
        if isinstance(refunds, list) and value("refunded_cents") is not None:
            settled_cents = sum(row["amount_cents"] for row in refunds if row["status"] == "SUCCEEDED")
            if settled_cents != value("refunded_cents"):
                conflicts.append("refunded_cents")
    else:
        items, fulfillments = value("order_items"), value("fulfillments")
        if isinstance(items, list) and isinstance(fulfillments, list):
            ordered = {row.get("sku"): row.get("quantity", 0) for row in items if isinstance(row, dict)}
            shipped = {sku: sum(row.get("quantity", 0) for row in fulfillments
                                if isinstance(row, dict) and row.get("sku") == sku)
                       for sku in ordered}
            if all(isinstance(qty, int) and qty >= 0 for qty in list(ordered.values()) + list(shipped.values())):
                short = any(shipped.get(sku, 0) < qty for sku, qty in ordered.items())
                confirmed = value("missing_item_confirmed")
                if confirmed is not None and short != confirmed:
                    conflicts.append("missing_item_confirmed")
        packages = value("packages")
        if isinstance(packages, list) and value("logistics_status") is not None:
            late = any(row["status"] == "LATE" for row in packages)
            if late != (value("logistics_status") == "LATE"):
                conflicts.append("logistics_status")
    return conflicts


def investigate(state: AgentState, role: str, allowed_types: set[str]) -> dict:
    task = next((task for task in state["tasks"] if task["role"] == role), None)
    if task is None:
        return {"role": role, "status": "SKIPPED", "task_id": None, "findings": [], "steps": 0}
    allowed_fields = set(task["allowed_fields"])
    tool = EvidenceSnapshotTool(allowed_fields, state["facts"], state["evidence_refs"])
    relevant = [issue for issue in state["issues"] if issue["issue_type"] in allowed_types]
    remaining = sorted(set().union(*(required_evidence(issue["issue_type"]) & allowed_fields for issue in relevant)))
    optional = {"fulfillment_investigator": {"order_items", "fulfillments", "packages"},
                "payment_investigator": {"payment_transactions", "refund_transactions"}}[role]
    remaining.extend(sorted(optional & state["facts"].keys()))
    observations = []
    observed = {}
    config = LLMConfig.from_env()
    while remaining and len(observations) < task["max_steps"]:
        field = select_investigation_field(role, task["issue_keys"], remaining, observations, config) if config else remaining[0]
        observation = tool.read(field)
        observed[field] = observation
        observations.append({"field": field, "status": observation["status"]})
        remaining.remove(field)
    findings = []
    domain_conflicts = _domain_conflicts(role, observed)
    for issue in relevant:
        fields = sorted(required_evidence(issue["issue_type"]) & allowed_fields)
        evidence_refs = []
        missing_fields = []
        for field in fields:
            observation = observed.get(field, {"status": "MISSING"})
            if observation["status"] == "AVAILABLE":
                if observation["evidence_ref"]:
                    evidence_refs.append(observation["evidence_ref"])
            else:
                missing_fields.append(field)
        findings.append({
            "issue_key": issue["key"],
            "status": "BLOCKED" if domain_conflicts else "NEEDS_INFO" if missing_fields else "EVIDENCE_READY",
            "conclusion": "UNKNOWN" if missing_fields or domain_conflicts else (
                "CONFIRMED" if (issue["issue_type"] == "MISSING_ITEM" and observed["missing_item_confirmed"]["value"] is True)
                or (issue["issue_type"] == "DUPLICATE_CHARGE" and observed["duplicate_charge_confirmed"]["value"] is True)
                or (issue["issue_type"] == "LATE_DELIVERY" and observed["logistics_status"]["value"] == "LATE")
                else "REFUTED"
            ),
            "missing_fields": missing_fields,
            "evidence_refs": evidence_refs,
            "conflicts": domain_conflicts,
            "domain_evidence_refs": [observed[field]["evidence_ref"] for field in sorted(optional)
                                     if field in observed and observed[field]["status"] == "AVAILABLE"
                                     and observed[field].get("evidence_ref")],
        })
    missing = sorted({field for finding in findings for field in finding["missing_fields"]})
    return {"role": role, "task_id": task["task_id"], "status": "BLOCKED" if domain_conflicts else "NEEDS_INFO" if missing else "COMPLETED", "findings": findings, "missing_fields": missing, "conflicts": domain_conflicts, "next_action": "TAKEOVER_CASE" if domain_conflicts else "COLLECT_EVIDENCE" if missing else "PLAN", "steps": len(observations)}
