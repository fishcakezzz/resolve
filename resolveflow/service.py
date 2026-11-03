import hashlib
import json

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .agents.graph import durable_graph
from .llm import LLMError
from .observability import tracer
from .models import AgentTask, InvestigationResult, ResolutionPlan, WorkflowCheckpoint, CaseTakeover, Approval, Case, Event, Evidence, Execution, Outbox, PolicyDecision, Proposal, Subcase, now
from .policy import POLICY_VERSION, evaluate, required_evidence
from .schemas import ApprovalIn, CaseCreate, EvidenceUpdate


class DomainError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def event(db: Session, case_id: str, event_type: str, payload: dict | None = None, subcase_id: str | None = None):
    db.add(Event(case_id=case_id, subcase_id=subcase_id, event_type=event_type, payload=payload or {}))


def get_case(db: Session, case_id: str) -> Case:
    item = db.get(Case, case_id)
    if item is None:
        raise DomainError("Case 不存在", 404)
    return item


def get_subcase(db: Session, case_id: str, key: str) -> Subcase:
    item = db.scalar(select(Subcase).where(Subcase.case_id == case_id, Subcase.key == key))
    if item is None:
        raise DomainError("Subcase 不存在", 404)
    return item


def facts_for(db: Session, case_id: str) -> dict:
    return {row.field: row.value for row in db.scalars(select(Evidence).where(Evidence.case_id == case_id, Evidence.status == "VALID"))}


def latest_proposal(db: Session, subcase_id: str) -> Proposal | None:
    return db.scalar(
        select(Proposal)
        .where(Proposal.subcase_id == subcase_id)
        .order_by(Proposal.version.desc())
        .limit(1)
    )


def validate_fact(field: str, value: object) -> None:
    if field in ("order_total_cents", "refunded_cents"):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DomainError(f"{field} 必须是非负整数分", 422)
    elif field == "order_owner":
        if not isinstance(value, str) or not value:
            raise DomainError("order_owner 必须是非空字符串", 422)
    elif field == "payment_status":
        if value not in ("PAID", "UNPAID", "REFUNDED", "CANCELLED"):
            raise DomainError("payment_status 无效", 422)
    elif field in ("missing_item_confirmed", "duplicate_charge_confirmed"):
        if not isinstance(value, bool):
            raise DomainError(f"{field} 必须是布尔值", 422)
    elif field == "logistics_status":
        if not isinstance(value, str) or not value:
            raise DomainError("logistics_status 必须是非空字符串", 422)
    elif field in ("order_items", "fulfillments"):
        if not isinstance(value, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("sku"), str) or not row["sku"]
            or isinstance(row.get("quantity"), bool) or not isinstance(row.get("quantity"), int)
            or row["quantity"] < 0 for row in value
        ):
            raise DomainError(f"{field} 必须包含 SKU 和非负整数数量", 422)
    elif field == "payment_transactions":
        if not isinstance(value, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("transaction_id"), str)
            or not row["transaction_id"] or row.get("status") not in ("SUCCEEDED", "FAILED", "PENDING")
            for row in value
        ):
            raise DomainError("payment_transactions 格式无效", 422)
    elif field == "refund_transactions":
        if not isinstance(value, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("refund_id"), str)
            or not row["refund_id"] or row.get("status") not in ("SUCCEEDED", "FAILED", "PENDING")
            or isinstance(row.get("amount_cents"), bool) or not isinstance(row.get("amount_cents"), int)
            or row["amount_cents"] < 0 for row in value
        ):
            raise DomainError("refund_transactions 格式无效", 422)
    elif field == "packages":
        if not isinstance(value, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("package_id"), str)
            or not row["package_id"] or row.get("status") not in ("IN_TRANSIT", "LATE", "DELIVERED", "LOST")
            for row in value
        ):
            raise DomainError("packages 格式无效", 422)


def create_case(db: Session, data: CaseCreate) -> Case:
    fingerprint = hashlib.sha256(json.dumps(data.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    existing = db.scalar(select(Case).where(Case.request_key == data.request_key))
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("请求幂等键已用于其他 Case")
        return existing
    item = Case(request_key=data.request_key, request_fingerprint=fingerprint, customer_id=data.customer_id, order_id=data.order_id)
    db.add(item)
    db.flush()
    for issue in data.issues:
        db.add(
            Subcase(
                case_id=item.id,
                key=issue.key,
                issue_type=issue.issue_type,
                requested_amount_cents=issue.requested_amount_cents,
                sku=issue.sku,
                depends_on=issue.depends_on,
            )
        )
    for field, fact in data.demo_facts.items():
        validate_fact(field, fact.value)
        db.add(
            Evidence(
                case_id=item.id,
                field=field,
                value=fact.value,
                source_system=fact.source_system,
                source_id=fact.source_id,
            )
        )
    event(db, item.id, "CASE_CREATED", {"issue_keys": [issue.key for issue in data.issues], "request_text": data.request_text})
    return item


def update_evidence(db: Session, case_id: str, field: str, data: EvidenceUpdate) -> Evidence:
    validate_fact(field, data.value)
    item = get_case(db, case_id)
    row = db.scalar(select(Evidence).where(Evidence.case_id == case_id, Evidence.field == field))
    if row:
        if row.value == data.value and row.source_id == data.source_id and row.source_system == data.source_system:
            return row
        row.value = data.value
        row.source_system = data.source_system
        row.source_id = data.source_id
        row.version += 1
        row.queried_at = now()
    else:
        row = Evidence(
            case_id=case_id,
            field=field,
            value=data.value,
            source_system=data.source_system,
            source_id=data.source_id,
        )
        db.add(row)
    item.evidence_version += 1
    _invalidate_unexecuted_proposals(db, item)
    item.status = "NEEDS_REVIEW"
    event(db, case_id, "EVIDENCE_UPDATED", {"field": field, "evidence_version": item.evidence_version})
    return row


def mark_evidence_conflict(db: Session, case_id: str, field: str) -> None:
    item = get_case(db, case_id)
    row = db.scalar(select(Evidence).where(Evidence.case_id == case_id, Evidence.field == field))
    if row and row.status != "CONFLICT":
        row.status = "CONFLICT"
        row.version += 1
        item.evidence_version += 1
        _invalidate_unexecuted_proposals(db, item)
        item.status = "NEEDS_REVIEW"
    event(db, case_id, "EVIDENCE_CONFLICT", {"field": field})


def _invalidate_unexecuted_proposals(db: Session, item: Case) -> None:
    for subcase in db.scalars(select(Subcase).where(Subcase.case_id == item.id)):
        proposal = latest_proposal(db, subcase.id)
        if proposal and proposal.status not in ("SUCCEEDED", "REJECTED", "DENIED"):
            proposal.status = "STALE"
            subcase.status = "NEEDS_REVIEW"
            outbox = db.scalar(select(Outbox).where(Outbox.proposal_id == proposal.id, Outbox.status == "PENDING"))
            if outbox:
                outbox.status = "CANCELLED"


def analyze(db: Session, case_id: str) -> Case:
    item = get_case(db, case_id)
    facts = facts_for(db, case_id)
    subcases = list(db.scalars(select(Subcase).where(Subcase.case_id == case_id).order_by(Subcase.key)))
    pending = []
    for subcase in subcases:
        current = latest_proposal(db, subcase.id)
        if current is None or current.status == "STALE":
            pending.append(subcase)
    if not pending:
        return item
    evidence_refs = {
        row.field: {"evidence_id": row.id, "version": row.version, "source_system": row.source_system, "source_id": row.source_id}
        for row in db.scalars(select(Evidence).where(Evidence.case_id == case_id, Evidence.status == "VALID"))
    }
    try:
        with tracer.start_as_current_span("case.analyze", attributes={"case.id": case_id, "evidence.version": item.evidence_version}), durable_graph() as graph:
            agent_result = graph.invoke({
                "case_id": case_id,
                "order_id": item.order_id,
                "evidence_version": item.evidence_version,
                "issues": [
                    {"key": subcase.key, "issue_type": subcase.issue_type, "requested_amount_cents": subcase.requested_amount_cents, "sku": subcase.sku}
                    for subcase in pending
                ],
                "facts": facts,
                "evidence_refs": evidence_refs,
            }, config={"configurable": {"thread_id": f"{case_id}:{item.evidence_version}"}})
    except LLMError as exc:
        raise DomainError(str(exc), 502) from exc
    checkpoint = db.get(WorkflowCheckpoint, case_id)
    if checkpoint is None:
        checkpoint = WorkflowCheckpoint(case_id=case_id, evidence_version=item.evidence_version,
                                        stage="ANALYZING", pending_issue_keys=[], missing_fields=[])
        db.add(checkpoint)
    checkpoint.evidence_version = item.evidence_version
    checkpoint.pending_issue_keys = [subcase.key for subcase in pending]
    checkpoint.missing_fields = agent_result["review"]["missing_fields"]
    checkpoint.stage = ("TAKEOVER_REQUIRED" if agent_result["review"]["status"] == "BLOCKED"
                        else "AWAITING_EVIDENCE" if checkpoint.missing_fields else "PLAN_READY")
    event(db, case_id, "AGENT_COORDINATOR_DISPATCHED", {"issue_keys": [subcase.key for subcase in pending], "task_ids": [task["task_id"] for task in agent_result["tasks"]]})
    for task in agent_result["tasks"]:
        db.add(AgentTask(id=task["task_id"], case_id=case_id, role=task["role"],
                         issue_keys=task["issue_keys"], objective=task["objective"],
                         order_ref=task["order_ref"], evidence_version=task["evidence_version"],
                         tool_scope=task["allowed_fields"], status="DISPATCHED"))
        event(db, case_id, "AGENT_TASK_DISPATCHED", {"task_id": task["task_id"], "role": task["role"], "issue_keys": task["issue_keys"], "evidence_version": task["evidence_version"]})
    for role_key in ("fulfillment_result", "payment_result"):
        result = agent_result[role_key]
        if result["status"] != "SKIPPED":
            task = db.get(AgentTask, result["task_id"])
            task.status = result["status"]
            db.add(InvestigationResult(task_id=result["task_id"], case_id=case_id,
                                       status=result["status"], findings=result["findings"],
                                       missing_fields=result["missing_fields"], conflicts=result["conflicts"],
                                       next_action=result["next_action"]))
            event(db, case_id, "AGENT_INVESTIGATION_COMPLETED", result)
    event(db, case_id, "AGENT_COORDINATOR_REVIEWED", agent_result["review"])
    previous_plan = db.scalar(select(ResolutionPlan).where(ResolutionPlan.case_id == case_id).order_by(ResolutionPlan.version.desc()))
    stored_plan = ResolutionPlan(case_id=case_id, version=previous_plan.version + 1 if previous_plan else 1,
                                 evidence_version=item.evidence_version, status=agent_result["plan"]["status"],
                                 actions=agent_result["plan"]["actions"], blockers=agent_result["plan"]["blockers"])
    db.add(stored_plan)
    planned_ids = []
    findings_by_key = {finding["issue_key"]: finding
                       for role_key in ("fulfillment_result", "payment_result")
                       for finding in agent_result[role_key].get("findings", [])}
    for subcase in pending:
        current = latest_proposal(db, subcase.id)
        result = agent_result["proposals"][subcase.key]
        proposal = Proposal(
            subcase_id=subcase.id,
            version=current.version + 1 if current else 1,
            action_type=result["action_type"],
            amount_cents=result["amount_cents"],
            sku=subcase.sku,
            evidence_version=item.evidence_version,
            reason=result["reason"],
        )
        db.add(proposal)
        db.flush()
        planned_ids.append(proposal.id)
        decision, reasons = evaluate(item, subcase, proposal, facts)
        finding = findings_by_key[subcase.key]
        if finding["conflicts"]:
            decision, reasons = "BLOCKED", ["调查证据互相冲突: " + ", ".join(finding["conflicts"])]
        elif finding["conclusion"] == "UNKNOWN":
            decision, reasons = "NEEDS_INFO", ["调查结论未知，需补充证据"]
        db.add(
            PolicyDecision(
                proposal_id=proposal.id,
                policy_version=POLICY_VERSION,
                decision=decision,
                reasons=reasons,
                evidence_version=item.evidence_version,
            )
        )
        proposal.status = {"ALLOW": "READY", "REVIEW": "PENDING_APPROVAL", "DENY": "DENIED", "NEEDS_INFO": "NEEDS_INFO", "BLOCKED": "NEEDS_REVIEW"}[decision]
        subcase.status = proposal.status
        event(db, case_id, "PROPOSAL_EVALUATED", {"proposal_id": proposal.id, "decision": decision, "reasons": reasons}, subcase.id)
    db.flush()
    preview = case_plan(db, case_id)
    stored_plan.actions = [{**action,
                            "finding": findings_by_key.get(action["subcase_key"], {}).get("conclusion")}
                           for action in preview["actions"]]
    action_blockers = [f"{action['subcase_key']}: {reason}" for action in preview["actions"]
                       for reason in action["blockers"]]
    stored_plan.blockers = list(dict.fromkeys(stored_plan.blockers + preview["global_blockers"] + action_blockers))
    if stored_plan.blockers:
        stored_plan.status = "BLOCKED"
        if checkpoint.stage == "PLAN_READY":
            checkpoint.stage = "PLAN_BLOCKED"
    event(db, case_id, "AGENT_PLAN_PROPOSED", {"proposal_ids": planned_ids, "plan_id": stored_plan.id,
                                              "evidence_version": item.evidence_version, "blockers": stored_plan.blockers})
    refresh_case_status(db, item)
    return item


def approve(db: Session, case_id: str, key: str, data: ApprovalIn) -> Proposal:
    item = get_case(db, case_id)
    subcase = get_subcase(db, case_id, key)
    proposal = latest_proposal(db, subcase.id)
    if proposal is None or proposal.status != "PENDING_APPROVAL":
        raise DomainError("当前 Subcase 无待审批方案")
    if proposal.evidence_version != item.evidence_version:
        raise DomainError("证据已更新，请重新分析")
    if data.decision == "MODIFY":
        if data.amount_cents is None:
            raise DomainError("修改方案必须提供 amount_cents", 422)
        proposal.status = "SUPERSEDED"
        replacement = Proposal(
            subcase_id=subcase.id,
            version=proposal.version + 1,
            action_type=proposal.action_type,
            amount_cents=data.amount_cents,
            sku=proposal.sku,
            evidence_version=item.evidence_version,
            reason=f"人工修改：{data.comment}",
        )
        db.add(replacement)
        db.flush()
        decision, reasons = evaluate(item, subcase, replacement, facts_for(db, case_id))
        db.add(PolicyDecision(proposal_id=replacement.id, policy_version=POLICY_VERSION, decision=decision, reasons=reasons, evidence_version=item.evidence_version))
        replacement.status = {"ALLOW": "READY", "REVIEW": "PENDING_APPROVAL", "DENY": "DENIED", "NEEDS_INFO": "NEEDS_INFO"}[decision]
        subcase.status = replacement.status
        db.add(Approval(proposal_id=proposal.id, proposal_version=proposal.version, approver_id=data.approver_id, decision="MODIFY", comment=data.comment))
        event(db, case_id, "PROPOSAL_MODIFIED", {"old_proposal_id": proposal.id, "new_proposal_id": replacement.id}, subcase.id)
        refresh_case_status(db, item)
        return replacement
    proposal.status = "READY" if data.decision == "APPROVE" else "REJECTED"
    subcase.status = proposal.status
    db.add(Approval(proposal_id=proposal.id, proposal_version=proposal.version, approver_id=data.approver_id, decision=data.decision, comment=data.comment))
    event(db, case_id, "APPROVAL_RECORDED", {"proposal_id": proposal.id, "decision": data.decision}, subcase.id)
    refresh_case_status(db, item)
    return proposal


def case_plan(db: Session, case_id: str) -> dict:
    """Read-only, explainable snapshot used by both preview and execution."""
    item = get_case(db, case_id)
    subcases = list(db.scalars(select(Subcase).where(Subcase.case_id == case_id).order_by(Subcase.key)))
    proposals = {subcase.key: latest_proposal(db, subcase.id) for subcase in subcases}
    facts = facts_for(db, case_id)
    actions = []
    for subcase in subcases:
        proposal = proposals[subcase.key]
        blockers = []
        if proposal is None:
            blockers.append("尚未生成候选方案")
        elif proposal.status == "SUCCEEDED":
            pass
        elif proposal.status != "READY":
            blockers.append(f"方案状态为 {proposal.status}")
        else:
            if proposal.evidence_version != item.evidence_version:
                blockers.append("方案使用的证据版本已过期")
            for parent in subcase.depends_on:
                parent_proposal = proposals.get(parent)
                if parent_proposal is None or parent_proposal.status != "SUCCEEDED":
                    blockers.append(f"依赖问题 {parent} 尚未完成")
            decision = db.scalar(select(PolicyDecision).where(PolicyDecision.proposal_id == proposal.id))
            if decision is None or decision.decision not in ("ALLOW", "REVIEW"):
                blockers.append("策略未授权执行")
            elif decision.decision == "REVIEW":
                approval = db.scalar(select(Approval).where(Approval.proposal_id == proposal.id, Approval.decision == "APPROVE"))
                if approval is None or approval.proposal_version != proposal.version:
                    blockers.append("缺少当前动作版本的有效审批")
        actions.append({
            "subcase_key": subcase.key,
            "issue_type": subcase.issue_type,
            "depends_on": subcase.depends_on,
            "proposal_id": proposal.id if proposal else None,
            "proposal_version": proposal.version if proposal else None,
            "action_type": proposal.action_type if proposal else None,
            "amount_cents": proposal.amount_cents if proposal else None,
            "sku": proposal.sku if proposal else None,
            "status": proposal.status if proposal else "UNPLANNED",
            "blockers": blockers,
            "ready_now": bool(proposal and proposal.status == "READY" and not blockers),
        })
    global_blockers = []
    required = {"order_owner", "payment_status", "order_total_cents", "refunded_cents"}
    if not required.issubset(facts) or facts.get("order_owner") != item.customer_id or facts.get("payment_status") != "PAID":
        global_blockers.append("执行前业务状态复核失败")
    active = [p for p in proposals.values() if p and p.status in ("READY", "PENDING_APPROVAL", "QUEUED", "EXECUTING", "SUCCEEDED")]
    total_refunds = sum(p.amount_cents for p in active if p.action_type == "REFUND")
    available = max(0, int(facts["order_total_cents"]) - int(facts["refunded_cents"])) if required.issubset(facts) else None
    if available is not None and total_refunds > available:
        global_blockers.append("整案累计退款超过可退余额")
    other_refunds = db.scalar(
        select(func.coalesce(func.sum(Proposal.amount_cents), 0))
        .join(Subcase, Proposal.subcase_id == Subcase.id)
        .join(Case, Subcase.case_id == Case.id)
        .where(Case.order_id == item.order_id, Case.id != item.id,
               Proposal.action_type == "REFUND",
               Proposal.status.in_(("QUEUED", "EXECUTING", "SUCCEEDED")))
    ) or 0
    if available is not None and total_refunds + other_refunds > available:
        global_blockers.append("跨 Case 累计退款超过订单可退余额")
    reships = [p.sku for p in active if p.action_type == "RESHIP"]
    if len(reships) != len(set(reships)):
        global_blockers.append("整案存在同一 SKU 的重复补发")
    refund_skus = {p.sku for p in active if p.action_type == "REFUND" and p.sku}
    if refund_skus.intersection(reships):
        global_blockers.append("整案存在同一 SKU 的退款与补发冲突")
    other_reships = set(db.scalars(
        select(Proposal.sku).join(Subcase, Proposal.subcase_id == Subcase.id)
        .join(Case, Subcase.case_id == Case.id)
        .where(Case.order_id == item.order_id, Case.id != item.id,
               Proposal.action_type == "RESHIP", Proposal.sku.is_not(None),
               Proposal.status.in_(("QUEUED", "EXECUTING", "SUCCEEDED")))
    ))
    if other_reships.intersection(reships):
        global_blockers.append("跨 Case 存在同一 SKU 的重复补发")
    if other_reships.intersection(refund_skus):
        global_blockers.append("跨 Case 存在同一 SKU 的退款与补发冲突")
    ready_keys = [action["subcase_key"] for action in actions if action["ready_now"]]
    takeover = db.get(CaseTakeover, case_id)
    if takeover and takeover.status == "ACTIVE" and (not takeover.issue_keys or set(ready_keys) & set(takeover.issue_keys)):
        global_blockers.append("人工接管范围内的动作已冻结")
    return {
        "case_id": case_id,
        "case_version": item.version,
        "evidence_version": item.evidence_version,
        "actions": actions,
        "total_refund_cents": total_refunds,
        "available_refund_cents": available,
        "other_case_refund_cents": other_refunds,
        "global_blockers": global_blockers,
        "ready_now": ready_keys,
        "can_execute_now": bool(ready_keys) and not global_blockers,
    }


def closure_proof(db: Session, case_id: str) -> dict:
    """Derive a verifiable closure checklist from persisted business records."""
    item = get_case(db, case_id)
    created = db.scalar(select(Event).where(Event.case_id == case_id, Event.event_type == "CASE_CREATED"))
    evidence = {row.field: row for row in db.scalars(select(Evidence).where(Evidence.case_id == case_id))}
    subcases = list(db.scalars(select(Subcase).where(Subcase.case_id == case_id).order_by(Subcase.key)))
    results = []
    for subcase in subcases:
        proposal = latest_proposal(db, subcase.id)
        decision = db.scalar(select(PolicyDecision).where(PolicyDecision.proposal_id == proposal.id)) if proposal else None
        approval = db.scalar(select(Approval).where(Approval.proposal_id == proposal.id)) if proposal else None
        execution = db.scalar(select(Execution).where(Execution.proposal_id == proposal.id)) if proposal else None
        outbox = db.scalar(select(Outbox).where(Outbox.proposal_id == proposal.id)) if proposal else None
        refs = []
        for field in sorted(required_evidence(subcase.issue_type)):
            row = evidence.get(field)
            refs.append({
                "field": field,
                "evidence_id": row.id if row else None,
                "source_system": row.source_system if row else None,
                "source_id": row.source_id if row else None,
                "version": row.version if row else None,
                "status": row.status if row else "MISSING",
                "queried_at": row.queried_at if row else None,
            })
        checks = {
            "subcase_succeeded": subcase.status == "SUCCEEDED",
            "evidence_valid": bool(proposal) and proposal.evidence_version == item.evidence_version and all(ref["status"] == "VALID" for ref in refs),
            "policy_authorized": bool(decision) and decision.decision in ("ALLOW", "REVIEW"),
            "approval_valid": bool(decision) and (decision.decision == "ALLOW" or bool(approval and approval.decision == "APPROVE" and approval.proposal_version == proposal.version)),
            "outbox_done": bool(outbox) and outbox.status == "DONE",
            "external_receipt_confirmed": bool(execution) and execution.status == "SUCCEEDED" and bool(execution.external_ref),
        }
        results.append({
            "subcase_key": subcase.key,
            "issue_type": subcase.issue_type,
            "status": subcase.status,
            "proposal_id": proposal.id if proposal else None,
            "proposal_version": proposal.version if proposal else None,
            "action_type": proposal.action_type if proposal else None,
            "amount_cents": proposal.amount_cents if proposal else None,
            "sku": proposal.sku if proposal else None,
            "policy_version": decision.policy_version if decision else None,
            "approval_id": approval.id if approval else None,
            "external_ref": execution.external_ref if execution else None,
            "evidence": refs,
            "checks": checks,
            "verified": all(checks.values()),
        })
    return {
        "case_id": case_id,
        "request_text": created.payload.get("request_text") if created else None,
        "case_status": item.status,
        "case_version": item.version,
        "evidence_version": item.evidence_version,
        "subcases": results,
        "verified": item.status == "SUCCEEDED" and bool(results) and all(row["verified"] for row in results),
    }


def queue_actions(db: Session, case_id: str, expected_version: int) -> int:
    item = get_case(db, case_id)
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:order_ref))"), {"order_ref": item.order_id})
    if item.version != expected_version:
        raise DomainError("Case 版本已变化，请刷新后重试")
    snapshot = case_plan(db, case_id)
    takeover = db.get(CaseTakeover, case_id)
    if takeover and takeover.status == "ACTIVE" and (not takeover.issue_keys or set(snapshot["ready_now"]) & set(takeover.issue_keys)):
        raise DomainError("人工接管范围内的动作已冻结")
    if snapshot["global_blockers"]:
        raise DomainError(snapshot["global_blockers"][0])
    if not snapshot["ready_now"]:
        raise DomainError("没有可执行动作")
    for key in snapshot["ready_now"]:
        subcase = get_subcase(db, case_id, key)
        proposal = latest_proposal(db, subcase.id)
        db.add(Outbox(proposal_id=proposal.id))
        proposal.status = "QUEUED"
        subcase.status = "QUEUED"
        event(db, case_id, "ACTION_QUEUED", {"proposal_id": proposal.id}, subcase.id)
    item.status = "EXECUTING"
    return len(snapshot["ready_now"])


def refresh_case_status(db: Session, item: Case) -> None:
    statuses = [row.status for row in db.scalars(select(Subcase).where(Subcase.case_id == item.id))]
    if statuses and all(status == "SUCCEEDED" for status in statuses):
        if item.status != "NEEDS_REVIEW":
            if item.status != "SUCCEEDED":
                event(db, item.id, "CASE_CLOSED", {"subcase_count": len(statuses), "evidence_version": item.evidence_version})
            item.status = "SUCCEEDED"
    elif any(status in ("QUEUED", "EXECUTING") for status in statuses):
        item.status = "EXECUTING"
    elif any(status == "PENDING_APPROVAL" for status in statuses):
        item.status = "PENDING_APPROVAL"
    elif any(status in ("NEEDS_INFO", "NEEDS_REVIEW") for status in statuses):
        item.status = "NEEDS_REVIEW"
    elif any(status == "READY" for status in statuses):
        item.status = "READY"
    elif any(status == "DENIED" for status in statuses):
        item.status = "NEEDS_REVIEW"
    elif any(status == "FAILED_NEEDS_REVIEW" for status in statuses):
        item.status = "FAILED_NEEDS_REVIEW"


def case_detail(db: Session, item: Case) -> dict:
    subcases = list(db.scalars(select(Subcase).where(Subcase.case_id == item.id).order_by(Subcase.key)))
    return {
        "id": item.id,
        "request_key": item.request_key,
        "customer_id": item.customer_id,
        "order_id": item.order_id,
        "status": item.status,
        "version": item.version,
        "evidence_version": item.evidence_version,
        "subcases": [
            {
                "key": subcase.key,
                "issue_type": subcase.issue_type,
                "status": subcase.status,
                "depends_on": subcase.depends_on,
                "proposal": _proposal_detail(db, latest_proposal(db, subcase.id)),
            }
            for subcase in subcases
        ],
    }


def _proposal_detail(db: Session, proposal: Proposal | None) -> dict | None:
    if proposal is None:
        return None
    decision = db.scalar(select(PolicyDecision).where(PolicyDecision.proposal_id == proposal.id))
    return {
        "id": proposal.id,
        "version": proposal.version,
        "action_type": proposal.action_type,
        "amount_cents": proposal.amount_cents,
        "sku": proposal.sku,
        "status": proposal.status,
        "reason": proposal.reason,
        "evidence_version": proposal.evidence_version,
        "policy": {"decision": decision.decision, "reasons": decision.reasons, "version": decision.policy_version} if decision else None,
    }
