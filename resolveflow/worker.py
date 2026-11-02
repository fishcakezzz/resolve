"""Outbox worker and idempotent local business-system simulator."""

import time
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from .db import transaction
from .models import Case, DemoAction, Evidence, Execution, Outbox, Proposal, Subcase, now
from .service import event, refresh_case_status


def _demo_external_action(idempotency_key: str, action_type: str, order_id: str, amount_cents: int, sku: str | None) -> str:
    """Separate transaction models an external service that remembers idempotency keys."""
    with transaction() as db:
        existing = db.scalar(select(DemoAction).where(DemoAction.idempotency_key == idempotency_key))
        if existing:
            return existing.id
        action = DemoAction(
            idempotency_key=idempotency_key,
            action_type=action_type,
            order_id=order_id,
            amount_cents=amount_cents,
            sku=sku,
        )
        db.add(action)
        db.flush()
        return action.id


def process_one() -> bool:
    """Return False when there is no pending event."""
    lease_expired_before = now() - timedelta(seconds=30)
    claimable = or_(
        Outbox.status == "PENDING",
        and_(Outbox.status == "PROCESSING", Outbox.claimed_at < lease_expired_before),
    )
    with transaction() as db:
        pending = db.scalar(select(Outbox).where(claimable).order_by(Outbox.created_at, Outbox.id).limit(1))
        if pending is None:
            return False
        changed = db.execute(
            update(Outbox)
            .where(Outbox.id == pending.id, claimable)
            .values(status="PROCESSING", attempts=Outbox.attempts + 1, claimed_at=now())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            return True
        outbox_id = pending.id

    with transaction() as db:
        outbox = db.get(Outbox, outbox_id)
        proposal = db.get(Proposal, outbox.proposal_id)
        subcase = db.get(Subcase, proposal.subcase_id)
        case = db.get(Case, subcase.case_id)
        if proposal.status not in ("QUEUED", "EXECUTING") or proposal.evidence_version != case.evidence_version:
            outbox.status = "CANCELLED"
            event(db, case.id, "ACTION_CANCELLED_STALE", {"proposal_id": proposal.id}, subcase.id)
            return True
        facts = {row.field: row.value for row in db.scalars(select(Evidence).where(Evidence.case_id == case.id, Evidence.status == "VALID"))}
        if facts.get("order_owner") != case.customer_id or facts.get("payment_status") != "PAID":
            outbox.status = "CANCELLED"
            proposal.status = "STALE"
            subcase.status = "NEEDS_REVIEW"
            refresh_case_status(db, case)
            event(db, case.id, "ACTION_CANCELLED_PREFLIGHT", {"proposal_id": proposal.id}, subcase.id)
            return True
        idempotency_key = f"{case.id}:{subcase.id}:{proposal.version}:{proposal.action_type}"
        existing_execution = db.scalar(select(Execution).where(Execution.proposal_id == proposal.id))
        if existing_execution and existing_execution.status == "SUCCEEDED":
            outbox.status = "DONE"
            proposal.status = "SUCCEEDED"
            subcase.status = "SUCCEEDED"
            refresh_case_status(db, case)
            return True
        if not existing_execution:
            db.add(Execution(proposal_id=proposal.id, idempotency_key=idempotency_key))
        proposal.status = "EXECUTING"
        subcase.status = "EXECUTING"
        case.status = "EXECUTING"
        order_id = case.order_id
        action_type = proposal.action_type
        amount_cents = proposal.amount_cents
        sku = proposal.sku

    try:
        external_ref = _demo_external_action(idempotency_key, action_type, order_id, amount_cents, sku)
    except Exception as exc:
        with transaction() as db:
            outbox = db.get(Outbox, outbox_id)
            outbox.status = "PENDING" if outbox.attempts < 3 else "FAILED"
            outbox.last_error = str(exc)[:500]
            proposal = db.get(Proposal, outbox.proposal_id)
            subcase = db.get(Subcase, proposal.subcase_id)
            case = db.get(Case, subcase.case_id)
            proposal.status = "QUEUED" if outbox.status == "PENDING" else "FAILED_NEEDS_REVIEW"
            subcase.status = proposal.status
            refresh_case_status(db, case)
            event(db, case.id, "ACTION_FAILED", {"proposal_id": proposal.id, "retry": outbox.status == "PENDING"}, subcase.id)
        return True

    with transaction() as db:
        outbox = db.get(Outbox, outbox_id)
        proposal = db.get(Proposal, outbox.proposal_id)
        subcase = db.get(Subcase, proposal.subcase_id)
        case = db.get(Case, subcase.case_id)
        execution = db.scalar(select(Execution).where(Execution.proposal_id == proposal.id))
        execution.status = "SUCCEEDED"
        execution.external_ref = external_ref
        outbox.status = "DONE"
        proposal.status = "SUCCEEDED"
        subcase.status = "SUCCEEDED"
        refresh_case_status(db, case)
        event(db, case.id, "ACTION_SUCCEEDED", {"proposal_id": proposal.id, "external_ref": external_ref}, subcase.id)
    return True


def main() -> None:
    from .db import init_db

    init_db()
    while True:
        if not process_one():
            time.sleep(1)


if __name__ == "__main__":
    main()
