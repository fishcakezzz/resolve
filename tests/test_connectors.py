import asyncio

import httpx

from resolveflow.connectors import collect


def test_parallel_sources_keep_valid_facts_and_flag_conflicts():
    def respond(request: httpx.Request):
        source = request.url.path.strip("/")
        if source == "crm":
            return httpx.Response(200, json={"source_id": "crm-1", "facts": {"order_owner": "customer-1"}})
        if source == "payment":
            return httpx.Response(200, json={"source_id": "payment-1", "facts": {"payment_status": "PAID", "order_total_cents": 10000}})
        if source == "oms":
            return httpx.Response(200, json={"source_id": "oms-1", "facts": {"order_total_cents": 9000}})
        return httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await collect(
                "customer-1",
                "order-1",
                urls={"CRM": "https://example.test/crm", "PAYMENT": "https://example.test/payment", "OMS": "https://example.test/oms", "LOGISTICS": "https://example.test/logistics"},
                client=client,
            )

    facts, errors = asyncio.run(run())
    assert {item.field for item in facts} == {"order_owner", "payment_status"}
    assert "CONFLICT:order_total_cents" in errors
    assert "LOGISTICS" in errors
