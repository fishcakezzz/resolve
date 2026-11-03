"""Optional idempotent business action HTTP adapter.

The remote service must deduplicate by idempotency_key and expose a status query.
"""

import os

import httpx


def configured() -> bool:
    return bool(os.getenv("RESOLVE_ACTION_API_URL") and os.getenv("RESOLVE_ACTION_STATUS_URL"))


def _headers() -> dict[str, str]:
    token = os.getenv("RESOLVE_ACTION_API_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def query_action(idempotency_key: str) -> str | None:
    """Return the confirmed external receipt or None for a still unknown action."""
    url = os.environ["RESOLVE_ACTION_STATUS_URL"]
    with httpx.Client(timeout=5) as client:
        response = client.get(url, params={"idempotency_key": idempotency_key}, headers=_headers())
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    if payload.get("status") == "SUCCEEDED" and isinstance(payload.get("external_ref"), str) and payload["external_ref"]:
        return payload["external_ref"]
    if payload.get("status") in ("PENDING", "UNKNOWN", "NOT_FOUND"):
        return None
    raise ValueError("外部动作状态不可确认，暂停重试")


def execute_action(idempotency_key: str, action_type: str, order_id: str,
                   amount_cents: int, sku: str | None) -> str:
    with httpx.Client(timeout=5) as client:
        response = client.post(
            os.environ["RESOLVE_ACTION_API_URL"], headers=_headers(),
            json={"idempotency_key": idempotency_key, "action_type": action_type,
                  "order_id": order_id, "amount_cents": amount_cents, "sku": sku},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("status") == "SUCCEEDED" and isinstance(payload.get("external_ref"), str) and payload["external_ref"]:
        return payload["external_ref"]
    receipt = query_action(idempotency_key)
    if receipt:
        return receipt
    raise ValueError("外部动作结果未知，等待状态查询")
