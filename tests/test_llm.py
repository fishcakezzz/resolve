import json

import httpx
import pytest

import resolveflow.llm as llm
from resolveflow.planner import plan


def configure(monkeypatch):
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("RESOLVE_LLM_MODEL", "test-model")
    monkeypatch.setenv("RESOLVE_LLM_API_KEY", "secret-test-key")


def test_chat_completions_request_and_validated_response(monkeypatch):
    configure(monkeypatch)
    captured = {}

    def respond(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"action_type": "REFUND", "amount_cents": 1000, "reason": "物流超时"})}}]},
        )

    monkeypatch.setattr(llm, "_new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(respond)))
    result = plan("LATE_DELIVERY", 1000, None, {"logistics_status": "LATE"})
    assert result["action_type"] == "REFUND"
    assert result["amount_cents"] == 1000
    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["auth"] == "Bearer secret-test-key"
    assert captured["body"]["model"] == "test-model"
    assert "secret-test-key" not in json.dumps(llm.public_status())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(200, json={"choices": [{"message": {"content": '{"action_type":"REFUND","amount_cents":"1000","reason":"bad"}'}}]}),
        httpx.Response(200, json={"choices": []}),
    ],
)
def test_model_errors_fail_closed(monkeypatch, response):
    configure(monkeypatch)
    monkeypatch.setattr(llm, "_new_http_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(lambda request: response)))
    with pytest.raises(llm.LLMError):
        llm.propose_with_model("LATE_DELIVERY", 1000, None, {})


def test_partial_configuration_is_visible_without_exposing_key(monkeypatch):
    monkeypatch.delenv("RESOLVE_LLM_CHAT_URL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.delenv("RESOLVE_LLM_MODEL", raising=False)
    monkeypatch.setenv("RESOLVE_LLM_API_KEY", "secret-test-key")
    status = llm.public_status()
    assert status["mode"] == "misconfigured"
    assert "secret-test-key" not in json.dumps(status)
