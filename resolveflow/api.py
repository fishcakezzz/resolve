from contextlib import asynccontextmanager
import os
from hmac import compare_digest

from fastapi import FastAPI, Header
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from .db import init_db, transaction
from .connectors import collect
from .agents.coordinator import draft as draft_case
from .llm import LLMError, public_status
from .observability import configure_telemetry
from .models import AgentTask, InvestigationResult, ResolutionPlan, WorkflowCheckpoint, CaseTakeover, Case, Event, Evidence, Execution, Outbox, Proposal, Subcase
from .schemas import ApprovalIn, CaseCreate, CaseDraftIn, EvidenceUpdate, ExecuteIn, TakeoverIn
from .service import DomainError, analyze, approve, case_detail, case_plan, closure_proof, create_case, event, get_case, mark_evidence_conflict, queue_actions, update_evidence


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_telemetry("resolveflow-api")
    init_db()
    yield


app = FastAPI(title="ResolveFlow", version="0.1.0", lifespan=lifespan)


def operator_identity(token: str | None) -> str:
    expected = os.getenv("RESOLVE_OPERATOR_TOKEN")
    operator_id = os.getenv("RESOLVE_OPERATOR_ID")
    if not expected or not operator_id:
        raise DomainError("未配置人工操作身份", 503)
    if not token or not compare_digest(token, expected):
        raise DomainError("人工操作令牌无效", 403)
    return operator_id


@app.exception_handler(DomainError)
async def handle_domain_error(request, exc: DomainError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(StaleDataError)
async def handle_stale_data(request, exc: StaleDataError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": "并发修改冲突，请刷新后重试"})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/llm/status")
def llm_status():
    return public_status()


@app.post("/api/case-drafts")
def post_case_draft(data: CaseDraftIn):
    try:
        return draft_case(data.request_text)
    except ValueError as exc:
        raise DomainError(str(exc), 422) from exc
    except LLMError as exc:
        raise DomainError(str(exc), 502) from exc


@app.post("/api/cases", status_code=201)
def post_case(data: CaseCreate):
    with transaction() as db:
        item = create_case(db, data)
        db.flush()
        return case_detail(db, item)


@app.get("/api/cases/{case_id}")
def read_case(case_id: str):
    with transaction() as db:
        return case_detail(db, get_case(db, case_id))


@app.get("/api/cases/{case_id}/plan")
def read_case_plan(case_id: str):
    with transaction() as db:
        return case_plan(db, case_id)


@app.get("/api/cases/{case_id}/closure-proof")
def read_closure_proof(case_id: str):
    with transaction() as db:
        return closure_proof(db, case_id)


@app.post("/api/cases/{case_id}/analyze")
def analyze_case(case_id: str):
    with transaction() as db:
        item = analyze(db, case_id)
        db.flush()
        return case_detail(db, item)


@app.post("/api/cases/{case_id}/resume")
def resume_case(case_id: str):
    with transaction() as db:
        item = get_case(db, case_id)
        checkpoint = db.get(WorkflowCheckpoint, case_id)
        if checkpoint is None:
            raise DomainError("尚无可恢复的分析进度")
        if checkpoint.stage in ("AWAITING_EVIDENCE", "TAKEOVER_REQUIRED") and checkpoint.evidence_version == item.evidence_version:
            raise DomainError("仍在等待补充证据")
        result = analyze(db, case_id)
        db.flush()
        return case_detail(db, result)


@app.post("/api/cases/{case_id}/takeover")
def take_over_case(case_id: str, data: TakeoverIn, x_operator_token: str | None = Header(default=None)):
    operator_id = operator_identity(x_operator_token)
    with transaction() as db:
        item = get_case(db, case_id)
        known = {subcase.key for subcase in db.scalars(select(Subcase).where(Subcase.case_id == case_id))}
        if set(data.issue_keys) - known:
            raise DomainError("接管范围包含不存在的 Subcase", 422)
        takeover = db.get(CaseTakeover, case_id)
        if takeover is None:
            takeover = CaseTakeover(case_id=case_id, operator_id=operator_id, reason=data.reason,
                                    issue_keys=data.issue_keys, status="ACTIVE")
            db.add(takeover)
        else:
            takeover.operator_id, takeover.reason, takeover.issue_keys, takeover.status = operator_id, data.reason, data.issue_keys, "ACTIVE"
        for outbox, proposal, subcase in db.execute(
            select(Outbox, Proposal, Subcase).join(Proposal, Outbox.proposal_id == Proposal.id)
            .join(Subcase, Proposal.subcase_id == Subcase.id)
            .where(Subcase.case_id == case_id, Outbox.status == "PENDING")
        ):
            if not data.issue_keys or subcase.key in data.issue_keys:
                outbox.status = "FROZEN"
        event(db, case_id, "CASE_TAKEN_OVER", {"operator_id": operator_id, "issue_keys": data.issue_keys, "reason": data.reason})
        db.flush()
        return {"case_id": item.id, "status": takeover.status, "issue_keys": takeover.issue_keys}


@app.post("/api/cases/{case_id}/release")
def release_case(case_id: str, x_operator_token: str | None = Header(default=None)):
    operator_id = operator_identity(x_operator_token)
    with transaction() as db:
        get_case(db, case_id)
        takeover = db.get(CaseTakeover, case_id)
        if takeover is None or takeover.status != "ACTIVE":
            raise DomainError("当前 Case 未被接管")
        takeover.status = "RELEASED"
        for outbox, proposal, subcase in db.execute(
            select(Outbox, Proposal, Subcase).join(Proposal, Outbox.proposal_id == Proposal.id)
            .join(Subcase, Proposal.subcase_id == Subcase.id)
            .where(Subcase.case_id == case_id, Outbox.status == "FROZEN")
        ):
            outbox.status = "PENDING"
        event(db, case_id, "CASE_RELEASED", {"operator_id": operator_id})
        return {"case_id": case_id, "status": takeover.status}


@app.get("/api/cases/{case_id}/workflow")
def read_workflow(case_id: str):
    with transaction() as db:
        get_case(db, case_id)
        checkpoint = db.get(WorkflowCheckpoint, case_id)
        tasks = list(db.scalars(select(AgentTask).where(AgentTask.case_id == case_id).order_by(AgentTask.created_at, AgentTask.id)))
        plans = list(db.scalars(select(ResolutionPlan).where(ResolutionPlan.case_id == case_id).order_by(ResolutionPlan.version)))
        return {
            "checkpoint": {"stage": checkpoint.stage, "evidence_version": checkpoint.evidence_version,
                           "pending_issue_keys": checkpoint.pending_issue_keys, "missing_fields": checkpoint.missing_fields}
                          if checkpoint else None,
            "tasks": [{"task_id": task.id, "role": task.role, "issue_keys": task.issue_keys,
                       "objective": task.objective, "order_ref": task.order_ref,
                       "evidence_version": task.evidence_version, "tool_scope": task.tool_scope,
                       "status": task.status,
                       "result": ({"status": result.status, "findings": result.findings,
                                   "missing_fields": result.missing_fields, "conflicts": result.conflicts,
                                   "next_action": result.next_action} if (result := db.scalar(select(InvestigationResult).where(InvestigationResult.task_id == task.id))) else None)}
                      for task in tasks],
            "plans": [{"version": plan.version, "evidence_version": plan.evidence_version,
                       "status": plan.status, "actions": plan.actions, "blockers": plan.blockers} for plan in plans],
        }


@app.get("/api/cases/{case_id}/evidence")
def read_evidence(case_id: str):
    with transaction() as db:
        get_case(db, case_id)
        rows = db.scalars(select(Evidence).where(Evidence.case_id == case_id).order_by(Evidence.field))
        return [
            {
                "field": row.field,
                "value": row.value,
                "source_system": row.source_system,
                "source_id": row.source_id,
                "version": row.version,
                "status": row.status,
                "queried_at": row.queried_at,
            }
            for row in rows
        ]


@app.put("/api/cases/{case_id}/evidence/{field}")
def put_evidence(case_id: str, field: str, data: EvidenceUpdate):
    with transaction() as db:
        row = update_evidence(db, case_id, field, data)
        db.flush()
        return {"field": row.field, "version": row.version, "case_evidence_version": get_case(db, case_id).evidence_version}


@app.post("/api/cases/{case_id}/collect-evidence")
async def collect_evidence(case_id: str):
    with transaction() as db:
        item = get_case(db, case_id)
        customer_id, order_id = item.customer_id, item.order_id
    collected, errors = await collect(customer_id, order_id)
    with transaction() as db:
        for fact in collected:
            update_evidence(db, case_id, fact.field, EvidenceUpdate(value=fact.value, source_system=fact.source_system, source_id=fact.source_id))
        for key in errors:
            if key.startswith("CONFLICT:"):
                mark_evidence_conflict(db, case_id, key.partition(":")[2])
        db.flush()
        return {"collected": len(collected), "errors": errors, "case_evidence_version": get_case(db, case_id).evidence_version}


@app.post("/api/cases/{case_id}/subcases/{key}/approval")
def post_approval(case_id: str, key: str, data: ApprovalIn, x_operator_token: str | None = Header(default=None)):
    if os.getenv("RESOLVE_OPERATOR_TOKEN"):
        if data.approver_id != operator_identity(x_operator_token):
            raise DomainError("审批身份与令牌不一致", 403)
    with transaction() as db:
        proposal = approve(db, case_id, key, data)
        db.flush()
        return {"proposal_id": proposal.id, "version": proposal.version, "status": proposal.status}


@app.post("/api/cases/{case_id}/execute")
def post_execute(case_id: str, data: ExecuteIn):
    with transaction() as db:
        count = queue_actions(db, case_id, data.expected_case_version)
        db.flush()
        return {"queued": count, "case": case_detail(db, get_case(db, case_id))}


@app.get("/api/cases/{case_id}/timeline")
def read_timeline(case_id: str):
    with transaction() as db:
        get_case(db, case_id)
        rows = db.scalars(select(Event).where(Event.case_id == case_id).order_by(Event.created_at, Event.id))
        return [
            {
                "event_type": row.event_type,
                "subcase_id": row.subcase_id,
                "payload": row.payload,
                "created_at": row.created_at,
            }
            for row in rows
        ]


@app.get("/api/cases/{case_id}/executions")
def read_executions(case_id: str):
    with transaction() as db:
        get_case(db, case_id)
        rows = db.execute(
            select(Execution, Proposal, Subcase)
            .join(Proposal, Execution.proposal_id == Proposal.id)
            .join(Subcase, Proposal.subcase_id == Subcase.id)
            .where(Subcase.case_id == case_id)
        )
        return [
            {
                "subcase_key": subcase.key,
                "proposal_id": proposal.id,
                "idempotency_key": execution.idempotency_key,
                "status": execution.status,
                "external_ref": execution.external_ref,
            }
            for execution, proposal, subcase in rows
        ]
