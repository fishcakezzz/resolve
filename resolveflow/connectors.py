"""Configurable read-only evidence adapters.

Each source URL must return {"source_id": str, "facts": {field: value}}.
Production systems normally need a custom adapter for their own API contract.
"""

import asyncio
import os
from dataclasses import dataclass

import httpx


SOURCES = ("CRM", "OMS", "PAYMENT", "LOGISTICS", "KNOWLEDGE")


@dataclass(frozen=True)
class CollectedFact:
    field: str
    value: object
    source_system: str
    source_id: str


def configured_sources() -> dict[str, str]:
    return {source: url for source in SOURCES if (url := os.getenv(f"RESOLVE_{source}_EVIDENCE_URL"))}


async def collect(customer_id: str, order_id: str, urls: dict[str, str] | None = None, client: httpx.AsyncClient | None = None) -> tuple[list[CollectedFact], dict[str, str]]:
    urls = urls if urls is not None else configured_sources()
    if not urls:
        return [], {"CONFIG": "未配置证据接口"}
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=3)

    async def fetch(source: str, url: str):
        try:
            response = await client.get(url, params={"customer_id": customer_id, "order_id": order_id})
            response.raise_for_status()
            payload = response.json()
            source_id = payload["source_id"]
            facts = payload["facts"]
            if not isinstance(source_id, str) or not source_id or not isinstance(facts, dict):
                raise ValueError("接口响应格式错误")
            return source, [CollectedFact(field, value, source, source_id) for field, value in facts.items()], None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            return source, [], str(exc)

    try:
        results = await asyncio.gather(*(fetch(source, url) for source, url in urls.items()))
    finally:
        if own_client:
            await client.aclose()
    collected: list[CollectedFact] = []
    errors: dict[str, str] = {}
    for source, facts, error in results:
        collected.extend(facts)
        if error:
            errors[source] = error
    by_field: dict[str, CollectedFact] = {}
    conflicts: set[str] = set()
    for fact in collected:
        previous = by_field.get(fact.field)
        if previous is not None and previous.value != fact.value:
            conflicts.add(fact.field)
        else:
            by_field[fact.field] = fact
    for field in sorted(conflicts):
        errors[f"CONFLICT:{field}"] = "不同系统返回了互相冲突的事实，需人工核查"
    return [fact for field, fact in by_field.items() if field not in conflicts], errors
