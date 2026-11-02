import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm import LLMError
from .models import Approval, Case, Event, Evidence, Execution, Outbox, PolicyDecision, Proposal, Subcase, now
from .planner import plan
from .policy import POLICY_VERSION, evaluate
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
    event(db, item.id, "CASE_CREATED", {"issue_keys": [issue.key for issue in data.issues]})
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
    for subcase in db.scalars(select(Subcase).where(Subcase.case_id == case_id).order_by(Subcase.key)):
        current = latest_proposal(db, subcase.id)
        if current and current.status != "STALE":
            continue
        try:
            result = plan(subcase.issue_type, subcase.requested_amount_cents, subcase.sku, facts)
        except LLMError as exc:
            raise DomainError(str(exc), 502) from exc
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
        decision, reasons = evaluate(item, subcase, proposal, facts)
        db.add(
            PolicyDecision(
                proposal_id=proposal.id,
                policy_version=POLICY_VERSION,
                decision=decision,
                reasons=reasons,
                evidence_version=item.evidence_version,
            )
        )
        proposal.status = {"ALLOW": "READY", "REVIEW": "PENDING_APPROVAL", "DENY": "DENIED", "NEEDS_INFO": "NEEDS_INFO"}[decision]
        subcase.status = proposal.status
        event(db, case_id, "PROPOSAL_EVALUATED", {"proposal_id": proposal.id, "decision": decision, "reasons": reasons}, subcase.id)
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


def queue_actions(db: Session, case_id: str, expected_version: int) -> int:
    item = get_case(db, case_id)
    if item.version != expected_version:
        raise DomainError("Case 版本已变化，请刷新后重试")
    subcases = list(db.scalars(select(Subcase).where(Subcase.case_id == case_id)))
    proposals = {subcase.key: latest_proposal(db, subcase.id) for subcase in subcases}
    ready = [
        (subcase, proposals[subcase.key])
        for subcase in subcases
        if proposals[subcase.key]
        and proposals[subcase.key].status == "READY"
        and all(proposals[parent] is not None and proposals[parent].status == "SUCCEEDED" for parent in subcase.depends_on)
    ]
    if not ready:
        raise DomainError("没有可执行动作")
    facts = facts_for(db, case_id)
    required = {"order_owner", "payment_status", "order_total_cents", "refunded_cents"}
    if not required.issubset(facts) or facts["order_owner"] != item.customer_id or facts["payment_status"] != "PAID":
        raise DomainError("执行前业务状态复核失败")
    for subcase, proposal in ready:
        if proposal.evidence_version != item.evidence_version:
            raise DomainError("方案使用的证据版本已过期")
        decision = db.scalar(select(PolicyDecision).where(PolicyDecision.proposal_id == proposal.id))
        if decision.decision == "REVIEW":
            approval = db.scalar(select(Approval).where(Approval.proposal_id == proposal.id, Approval.decision == "APPROVE"))
            if approval is None or approval.proposal_version != proposal.version:
                raise DomainError("缺少当前动作版本的有效审批")
        elif decision.decision != "ALLOW":
            raise DomainError("策略未授权执行")
    active_refunds = sum(
        proposal.amount_cents
        for proposal in proposals.values()
        if proposal and proposal.action_type == "REFUND" and proposal.status in ("READY", "PENDING_APPROVAL", "QUEUED", "EXECUTING", "SUCCEEDED")
    )
    if active_refunds > int(facts["order_total_cents"]) - int(facts["refunded_cents"]):
        raise DomainError("整案累计退款超过可退余额")
    active_reships = [
        proposal.sku
        for proposal in proposals.values()
        if proposal and proposal.action_type == "RESHIP" and proposal.status in ("READY", "PENDING_APPROVAL", "QUEUED", "EXECUTING", "SUCCEEDED")
    ]
    if len(active_reships) != len(set(active_reships)):
        raise DomainError("整案存在同一 SKU 的重复补发")
    for subcase, proposal in ready:
        db.add(Outbox(proposal_id=proposal.id))
        proposal.status = "QUEUED"
        subcase.status = "QUEUED"
        event(db, case_id, "ACTION_QUEUED", {"proposal_id": proposal.id}, subcase.id)
    item.status = "EXECUTING"
    return len(ready)


def refresh_case_status(db: Session, item: Case) -> None:
    statuses = [row.status for row in db.scalars(select(Subcase).where(Subcase.case_id == item.id))]
    if statuses and all(status == "SUCCEEDED" for status in statuses):
        if item.status != "NEEDS_REVIEW":
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
