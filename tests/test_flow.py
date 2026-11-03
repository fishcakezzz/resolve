import os
import json
import tempfile
from datetime import timedelta
from pathlib import Path

_db_path = Path(tempfile.mkdtemp()) / "test_resolveflow.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path.as_posix()}"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
import pytest  # noqa: E402

from resolveflow.api import app  # noqa: E402
from resolveflow.db import transaction  # noqa: E402
from resolveflow.models import DemoAction, Outbox, now  # noqa: E402
from resolveflow.worker import process_one  # noqa: E402
import resolveflow.worker as worker_module  # noqa: E402
import resolveflow.api as api_module  # noqa: E402
import resolveflow.llm as llm_module  # noqa: E402
import httpx  # noqa: E402


def fact(value, source="OMS"):
    return {"value": value, "source_system": source, "source_id": f"demo-{source.lower()}"}


def base_facts(total=10000):
    return {
        "order_owner": fact("customer-1", "CRM"),
        "payment_status": fact("PAID", "PAYMENT"),
        "order_total_cents": fact(total),
        "refunded_cents": fact(0, "PAYMENT"),
        "missing_item_confirmed": fact(True),
        "logistics_status": fact("LATE", "LOGISTICS"),
        "duplicate_charge_confirmed": fact(True, "PAYMENT"),
    }


def create(client, request_key, issues, facts=None):
    response = client.post(
        "/api/cases",
        json={
            "request_key": request_key,
            "customer_id": "customer-1",
            "order_id": f"order-{request_key}",
            "issues": issues,
            "demo_facts": facts if facts is not None else base_facts(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_complex_case_closes_only_after_all_actions():
    with TestClient(app) as client:
        case = create(
            client,
            "complex-1",
            [
                {"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-1"},
                {"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000},
                {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 4000},
            ],
        )
        case_id = case["id"]
        analyzed = client.post(f"/api/cases/{case_id}/analyze")
        assert analyzed.status_code == 200, analyzed.text
        current = analyzed.json()
        assert current["status"] == "READY"
        queued = client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]})
        assert queued.status_code == 200, queued.text
        assert queued.json()["queued"] == 3
        assert process_one() is True
        assert client.get(f"/api/cases/{case_id}").json()["status"] == "EXECUTING"
        assert process_one() is True
        assert process_one() is True
        assert process_one() is False
        assert client.get(f"/api/cases/{case_id}").json()["status"] == "SUCCEEDED"
        assert len(client.get(f"/api/cases/{case_id}/executions").json()) == 3
        with transaction() as db:
            assert db.scalar(select(func.count()).select_from(DemoAction)) == 3


def test_evidence_change_invalidates_approval_and_requires_new_version():
    with TestClient(app) as client:
        facts = base_facts()
        facts.pop("duplicate_charge_confirmed")
        case = create(client, "evidence-1", [{"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 6000}], facts)
        case_id = case["id"]
        first = client.post(f"/api/cases/{case_id}/analyze").json()
        assert first["subcases"][0]["status"] == "NEEDS_INFO"
        result = client.put(f"/api/cases/{case_id}/evidence/duplicate_charge_confirmed", json=fact(True, "PAYMENT"))
        assert result.status_code == 200
        second = client.post(f"/api/cases/{case_id}/analyze").json()
        assert second["subcases"][0]["proposal"]["version"] == 2
        assert second["subcases"][0]["status"] == "PENDING_APPROVAL"
        client.put(f"/api/cases/{case_id}/evidence/payment_status", json={"value": "PAID", "source_system": "PAYMENT", "source_id": "new-payment-record"})
        refused = client.post(f"/api/cases/{case_id}/subcases/double/approval", json={"approver_id": "manager-1", "decision": "APPROVE"})
        assert refused.status_code == 409
        third = client.post(f"/api/cases/{case_id}/analyze").json()
        assert third["subcases"][0]["proposal"]["version"] == 3
        approved = client.post(f"/api/cases/{case_id}/subcases/double/approval", json={"approver_id": "manager-1", "decision": "APPROVE"})
        assert approved.status_code == 200
        ready = client.get(f"/api/cases/{case_id}").json()
        assert client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": ready["version"]}).status_code == 200
        assert process_one() is True
        assert client.get(f"/api/cases/{case_id}").json()["status"] == "SUCCEEDED"


def test_whole_case_refund_budget_blocks_locally_valid_actions():
    with TestClient(app) as client:
        case = create(
            client,
            "budget-1",
            [
                {"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 4000},
                {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 7000},
            ],
        )
        case_id = case["id"]
        client.post(f"/api/cases/{case_id}/analyze")
        client.post(f"/api/cases/{case_id}/subcases/double/approval", json={"approver_id": "manager-1", "decision": "APPROVE"})
        current = client.get(f"/api/cases/{case_id}").json()
        blocked = client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]})
        assert blocked.status_code == 409
        assert "整案累计退款" in blocked.json()["detail"]
        assert process_one() is False


def test_dependency_and_request_idempotency():
    with TestClient(app) as client:
        issues = [
            {"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-2"},
            {"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000, "depends_on": ["missing"]},
        ]
        case = create(client, "dependency-1", issues)
        repeated = create(client, "dependency-1", issues)
        assert repeated["id"] == case["id"]
        conflict = client.post(
            "/api/cases",
            json={"request_key": "dependency-1", "customer_id": "customer-1", "order_id": "order-1", "issues": [{"key": "other", "issue_type": "MISSING_ITEM", "sku": "SKU-3"}], "demo_facts": base_facts()},
        )
        assert conflict.status_code == 409
        case_id = case["id"]
        current = client.post(f"/api/cases/{case_id}/analyze").json()
        first = client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]})
        assert first.status_code == 200, first.text
        assert first.json()["queued"] == 1
        assert process_one() is True
        current = client.get(f"/api/cases/{case_id}").json()
        second = client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]})
        assert second.status_code == 200, second.text
        assert second.json()["queued"] == 1
        assert process_one() is True
        assert client.get(f"/api/cases/{case_id}").json()["status"] == "SUCCEEDED"
        with transaction() as db:
            assert db.scalar(select(func.count()).select_from(DemoAction)) >= 2


def test_worker_recovers_after_external_success_before_receipt(monkeypatch):
    with TestClient(app) as client:
        case = create(client, "crash-1", [{"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-CRASH"}])
        case_id = case["id"]
        analyzed = client.post(f"/api/cases/{case_id}/analyze").json()
        queued = client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": analyzed["version"]})
        assert queued.status_code == 200
        original = worker_module._demo_external_action

        def crash_after_external(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt("simulated worker crash")

        monkeypatch.setattr(worker_module, "_demo_external_action", crash_after_external)
        with pytest.raises(KeyboardInterrupt):
            process_one()
        monkeypatch.setattr(worker_module, "_demo_external_action", original)
        with transaction() as db:
            outbox = db.scalar(select(Outbox).where(Outbox.status == "PROCESSING"))
            assert outbox is not None
            outbox.claimed_at = now() - timedelta(seconds=31)
        assert process_one() is True
        assert client.get(f"/api/cases/{case_id}").json()["status"] == "SUCCEEDED"
        with transaction() as db:
            assert db.scalar(select(func.count()).select_from(DemoAction).where(DemoAction.order_id == "order-crash-1", DemoAction.sku == "SKU-CRASH")) == 1


def test_conflicting_source_invalidates_existing_proposal(monkeypatch):
    async def conflicting_collect(customer_id, order_id):
        return [], {"CONFLICT:order_total_cents": "不同系统返回了互相冲突的事实，需人工核查"}

    monkeypatch.setattr(api_module, "collect", conflicting_collect)
    with TestClient(app) as client:
        case = create(client, "conflict-1", [{"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000}])
        case_id = case["id"]
        assert client.post(f"/api/cases/{case_id}/analyze").json()["subcases"][0]["status"] == "READY"
        collected = client.post(f"/api/cases/{case_id}/collect-evidence")
        assert collected.status_code == 200
        detail = client.get(f"/api/cases/{case_id}").json()
        assert detail["subcases"][0]["status"] == "NEEDS_REVIEW"
        evidence = {row["field"]: row for row in client.get(f"/api/cases/{case_id}/evidence").json()}
        assert evidence["order_total_cents"]["status"] == "CONFLICT"


def test_invalid_model_response_does_not_create_executable_proposal(monkeypatch):
    monkeypatch.setenv("RESOLVE_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_MODEL", "test-model")
    monkeypatch.setenv("RESOLVE_LLM_API_KEY", "secret-test-key")
    response = httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
    monkeypatch.setattr(llm_module, "_new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda request: response)))
    with TestClient(app) as client:
        case = create(client, "bad-llm-1", [{"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000}])
        case_id = case["id"]
        failed = client.post(f"/api/cases/{case_id}/analyze")
        assert failed.status_code == 502
        current = client.get(f"/api/cases/{case_id}").json()
        assert current["subcases"][0]["proposal"] is None
        assert client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]}).status_code == 409


def test_model_cannot_authorize_refund_above_subcase_amount(monkeypatch):
    monkeypatch.setenv("RESOLVE_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_MODEL", "test-model")
    monkeypatch.setenv("RESOLVE_LLM_API_KEY", "secret-test-key")
    def model_reply(request):
        user_payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "remaining_fields" in user_payload:
            content = json.dumps({"field": user_payload["remaining_fields"][0]})
        else:
            content = '{"action_type":"REFUND","amount_cents":9000,"reason":"模型建议退款"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(llm_module, "_new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(model_reply)))
    with TestClient(app) as client:
        case = create(client, "over-llm-1", [{"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000}])
        result = client.post(f"/api/cases/{case['id']}/analyze")
        assert result.status_code == 200
        proposal = result.json()["subcases"][0]["proposal"]
        assert proposal["status"] == "DENIED"
        assert any("超过该问题申报金额" in reason for reason in proposal["policy"]["reasons"])


def test_natural_language_draft_requires_confirmation_and_exposes_missing_inputs(monkeypatch):
    monkeypatch.delenv("RESOLVE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    with TestClient(app) as client:
        draft = client.post("/api/case-drafts", json={"request_text": "订单少发一件，物流晚到，还重复扣款"})
        assert draft.status_code == 200, draft.text
        body = draft.json()
        assert body["confirmation_required"] is True
        assert {row["issue_type"] for row in body["issues"]} == {"MISSING_ITEM", "LATE_DELIVERY", "DUPLICATE_CHARGE"}
        assert any(row["needs_input"] for row in body["issues"])
        assert client.post("/api/case-drafts", json={"request_text": "请帮我看看"}).status_code == 422
        confirmed = client.post("/api/cases", json={
            "request_key": "draft-confirm-1", "customer_id": "customer-1", "order_id": "order-1",
            "request_text": body["request_text"], "issues": body["issues"], "demo_facts": base_facts(),
        })
        assert confirmed.status_code == 201, confirmed.text
        proof = client.get(f"/api/cases/{confirmed.json()['id']}/closure-proof").json()
        assert proof["request_text"] == body["request_text"]
        assert proof["verified"] is False


def test_plan_explains_whole_case_conflict_and_blocks_execution():
    with TestClient(app) as client:
        case = create(client, "plan-conflict-1", [
            {"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 4000},
            {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 7000},
        ])
        case_id = case["id"]
        client.post(f"/api/cases/{case_id}/analyze")
        plan = client.get(f"/api/cases/{case_id}/plan").json()
        assert plan["total_refund_cents"] == 11000
        assert plan["available_refund_cents"] == 10000
        assert "整案累计退款超过可退余额" in plan["global_blockers"]
        assert plan["can_execute_now"] is False
        approved = client.post(f"/api/cases/{case_id}/subcases/double/approval", json={"approver_id": "manager-1", "decision": "APPROVE"})
        assert approved.status_code == 200
        current = client.get(f"/api/cases/{case_id}").json()
        assert client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]}).status_code == 409


def test_closure_proof_requires_every_receipt_and_evidence():
    with TestClient(app) as client:
        case = create(client, "proof-1", [
            {"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-PROOF"},
            {"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000},
        ])
        case_id = case["id"]
        analyzed = client.post(f"/api/cases/{case_id}/analyze").json()
        assert client.get(f"/api/cases/{case_id}/plan").json()["can_execute_now"] is True
        assert client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": analyzed["version"]}).status_code == 200
        assert process_one() is True
        partial = client.get(f"/api/cases/{case_id}/closure-proof").json()
        assert partial["verified"] is False
        assert sum(row["verified"] for row in partial["subcases"]) == 1
        assert process_one() is True
        proof = client.get(f"/api/cases/{case_id}/closure-proof").json()
        assert proof["verified"] is True
        assert all(row["checks"]["external_receipt_confirmed"] for row in proof["subcases"])
        assert all(row["evidence"] and all(ref["source_id"] for ref in row["evidence"]) for row in proof["subcases"])
        assert any(row["event_type"] == "CASE_CLOSED" for row in client.get(f"/api/cases/{case_id}/timeline").json())


def test_model_draft_is_schema_checked_before_confirmation(monkeypatch):
    monkeypatch.setenv("RESOLVE_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_MODEL", "test-model")
    monkeypatch.setenv("RESOLVE_LLM_API_KEY", "secret-test-key")
    content = '{"issues":[{"issue_type":"MISSING_ITEM","requested_amount_cents":0,"sku":"SKU-1","depends_on":[],"reason":"少发商品"},{"issue_type":"LATE_DELIVERY","requested_amount_cents":500,"sku":null,"depends_on":[0],"reason":"到货延误"}]}'
    response = httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr("resolveflow.decomposition._new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda request: response)))
    with TestClient(app) as client:
        draft = client.post("/api/case-drafts", json={"request_text": "少发 SKU-1，之后物流又延误"})
        assert draft.status_code == 200, draft.text
        issues = draft.json()["issues"]
        assert issues[1]["depends_on"] == [issues[0]["key"]]
        invalid = httpx.Response(200, json={"choices": [{"message": {"content": '{"issues":[{"issue_type":"REFUND","reason":"越权"}]}'}}]})
        monkeypatch.setattr("resolveflow.decomposition._new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda request: invalid)))
        assert client.post("/api/case-drafts", json={"request_text": "退钱"}).status_code == 502


def test_four_role_graph_dispatches_scoped_investigations():
    with TestClient(app) as client:
        case = create(client, "agent-roles-1", [
            {"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-1"},
            {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 1000},
        ])
        case_id = case["id"]
        analyzed = client.post(f"/api/cases/{case_id}/analyze")
        assert analyzed.status_code == 200, analyzed.text
        assert all(row["proposal"] for row in analyzed.json()["subcases"])
        events = client.get(f"/api/cases/{case_id}/timeline").json()
        tasks = [row["payload"] for row in events if row["event_type"] == "AGENT_TASK_DISPATCHED"]
        assert {task["role"] for task in tasks} == {"fulfillment_investigator", "payment_investigator"}
        results = [row["payload"] for row in events if row["event_type"] == "AGENT_INVESTIGATION_COMPLETED"]
        assert len(results) == 2
        for result in results:
            assert result["steps"] <= 8
            refs = [ref for finding in result["findings"] for ref in finding["evidence_refs"]]
            assert refs
            sources = {ref["source_system"] for ref in refs}
            if result["role"] == "payment_investigator":
                assert sources == {"PAYMENT"}
            else:
                assert "PAYMENT" not in sources


def test_investigator_tool_denies_out_of_scope_fields():
    from resolveflow.agents.contracts import EvidenceSnapshotTool

    tool = EvidenceSnapshotTool({"payment_status"}, {"payment_status": "PAID", "order_owner": "customer-1"}, {})
    assert tool.read("payment_status")["value"] == "PAID"
    with pytest.raises(ValueError):
        tool.read("order_owner")


def test_model_investigator_cannot_read_other_roles_evidence(monkeypatch):
    monkeypatch.setenv("RESOLVE_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_MODEL", "test-model")
    monkeypatch.setenv("RESOLVE_LLM_API_KEY", "secret-test-key")
    bad = httpx.Response(200, json={"choices": [{"message": {"content": '{"field":"payment_status"}'}}]})
    monkeypatch.setattr(llm_module, "_new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda request: bad)))
    with TestClient(app) as client:
        case = create(client, "agent-scope-1", [{"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-1"}])
        refused = client.post(f"/api/cases/{case['id']}/analyze")
        assert refused.status_code == 502
        assert client.get(f"/api/cases/{case['id']}").json()["subcases"][0]["proposal"] is None


def test_persisted_tasks_findings_and_resume_after_new_evidence():
    with TestClient(app) as client:
        facts = base_facts()
        facts.pop("duplicate_charge_confirmed")
        case = create(client, "resume-contract-1", [
            {"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-R"},
            {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 1000},
        ], facts)
        case_id = case["id"]
        client.post(f"/api/cases/{case_id}/analyze")
        workflow = client.get(f"/api/cases/{case_id}/workflow").json()
        assert workflow["checkpoint"]["stage"] == "AWAITING_EVIDENCE"
        assert workflow["checkpoint"]["missing_fields"] == ["duplicate_charge_confirmed"]
        assert {task["role"] for task in workflow["tasks"]} == {"fulfillment_investigator", "payment_investigator"}
        assert {finding["conclusion"] for task in workflow["tasks"] for finding in task["result"]["findings"]} == {"CONFIRMED", "UNKNOWN"}
        assert client.post(f"/api/cases/{case_id}/resume").status_code == 409
        client.put(f"/api/cases/{case_id}/evidence/duplicate_charge_confirmed", json=fact(True, "PAYMENT"))
        assert client.post(f"/api/cases/{case_id}/resume").status_code == 200
        resumed = client.get(f"/api/cases/{case_id}/workflow").json()
        assert resumed["checkpoint"]["stage"] == "PLAN_READY"
        assert [plan["version"] for plan in resumed["plans"]] == [1, 2]
        assert resumed["plans"][-1]["status"] == "PROPOSED"
        from resolveflow.agents.graph import durable_graph
        evidence_version = client.get(f"/api/cases/{case_id}").json()["evidence_version"]
        with durable_graph() as graph:
            state = graph.get_state({"configurable": {"thread_id": f"{case_id}:{evidence_version}"}})
        assert state.values["plan"]["status"] == "PROPOSED"


def test_structured_payment_conflict_blocks_refund():
    with TestClient(app) as client:
        facts = base_facts()
        facts["payment_transactions"] = fact([
            {"transaction_id": "tx-1", "status": "SUCCEEDED"},
        ], "PAYMENT")
        case = create(client, "payment-conflict-1", [
            {"key": "double", "issue_type": "DUPLICATE_CHARGE", "requested_amount_cents": 1000},
        ], facts)
        case_id = case["id"]
        current = client.post(f"/api/cases/{case_id}/analyze").json()
        assert current["subcases"][0]["status"] == "NEEDS_REVIEW"
        workflow = client.get(f"/api/cases/{case_id}/workflow").json()
        assert workflow["tasks"][0]["result"]["findings"][0]["conclusion"] == "UNKNOWN"
        assert workflow["tasks"][0]["result"]["conflicts"] == ["duplicate_charge_confirmed"]
        assert client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": current["version"]}).status_code == 409


def test_cross_case_order_refund_budget():
    with TestClient(app) as client:
        first = create(client, "shared-order-a", [{"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 7000}])
        second = create(client, "shared-order-b", [{"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 7000}])
        from resolveflow.db import transaction
        from resolveflow.models import Case
        with transaction() as db:
            db.get(Case, second["id"]).order_id = f"order-shared-order-a"
        a = client.post(f"/api/cases/{first['id']}/analyze").json()
        b = client.post(f"/api/cases/{second['id']}/analyze").json()
        client.post(f"/api/cases/{first['id']}/subcases/late/approval", json={"approver_id": "manager-1", "decision": "APPROVE"})
        client.post(f"/api/cases/{second['id']}/subcases/late/approval", json={"approver_id": "manager-1", "decision": "APPROVE"})
        a = client.get(f"/api/cases/{first['id']}").json()
        b = client.get(f"/api/cases/{second['id']}").json()
        assert client.post(f"/api/cases/{first['id']}/execute", json={"expected_case_version": a["version"]}).status_code == 200
        plan = client.get(f"/api/cases/{second['id']}/plan").json()
        assert plan["other_case_refund_cents"] == 7000
        assert "跨 Case 累计退款超过订单可退余额" in plan["global_blockers"]
        assert client.post(f"/api/cases/{second['id']}/execute", json={"expected_case_version": b["version"]}).status_code == 409
        assert process_one() is True


def test_operator_takeover_freezes_queued_action_until_release(monkeypatch):
    monkeypatch.setenv("RESOLVE_OPERATOR_TOKEN", "test-operator-secret")
    monkeypatch.setenv("RESOLVE_OPERATOR_ID", "operator-1")
    headers = {"X-Operator-Token": "test-operator-secret"}
    with TestClient(app) as client:
        case = create(client, "takeover-1", [{"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-T"}])
        case_id = case["id"]
        ready = client.post(f"/api/cases/{case_id}/analyze").json()
        assert client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": ready["version"]}).status_code == 200
        assert client.post(f"/api/cases/{case_id}/takeover", json={"reason": "人工核查"}).status_code == 403
        assert client.post(f"/api/cases/{case_id}/takeover", json={"reason": "人工核查"}, headers=headers).status_code == 200
        assert process_one() is False
        assert client.post(f"/api/cases/{case_id}/release", headers=headers).status_code == 200
        assert process_one() is True
        assert client.get(f"/api/cases/{case_id}").json()["status"] == "SUCCEEDED"


def test_external_status_is_queried_before_replay(monkeypatch):
    monkeypatch.setenv("RESOLVE_ACTION_API_URL", "https://actions.example.test/execute")
    monkeypatch.setenv("RESOLVE_ACTION_STATUS_URL", "https://actions.example.test/status")
    monkeypatch.setattr(worker_module, "query_action", lambda key: "receipt-existing")
    def unexpected_execute(*args):
        raise AssertionError("已确认外部结果时不得再次发起动作")
    monkeypatch.setattr(worker_module, "execute_action", unexpected_execute)
    with TestClient(app) as client:
        case = create(client, "external-query-1", [{"key": "missing", "issue_type": "MISSING_ITEM", "sku": "SKU-E"}])
        case_id = case["id"]
        ready = client.post(f"/api/cases/{case_id}/analyze").json()
        client.post(f"/api/cases/{case_id}/execute", json={"expected_case_version": ready["version"]})
        assert process_one() is True
        executions = client.get(f"/api/cases/{case_id}/executions").json()
        assert executions[0]["external_ref"] == "receipt-existing"
