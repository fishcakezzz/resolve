import os
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
            "order_id": "order-1",
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
            assert db.scalar(select(func.count()).select_from(DemoAction).where(DemoAction.order_id == "order-1", DemoAction.sku == "SKU-CRASH")) == 1


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
    response = httpx.Response(200, json={"choices": [{"message": {"content": '{"action_type":"REFUND","amount_cents":9000,"reason":"模型建议退款"}'}}]})
    monkeypatch.setattr(llm_module, "_new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda request: response)))
    with TestClient(app) as client:
        case = create(client, "over-llm-1", [{"key": "late", "issue_type": "LATE_DELIVERY", "requested_amount_cents": 1000}])
        result = client.post(f"/api/cases/{case['id']}/analyze")
        assert result.status_code == 200
        proposal = result.json()["subcases"][0]["proposal"]
        assert proposal["status"] == "DENIED"
        assert any("超过该问题申报金额" in reason for reason in proposal["policy"]["reasons"])
