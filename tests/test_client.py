from __future__ import annotations

import json

import httpx
import pytest

from aiday.client import Client


def test_write_retries_5xx_with_same_idempotency_key() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(503, json={"detail": "try again"})
        return httpx.Response(200, json={"action_id": "act_1"})

    with Client(
        "http://aiday.test", "token", transport=httpx.MockTransport(handler),
        max_retries=2, backoff_base=0, backoff_max=0,
    ) as client:
        receipt = client.cancel_order("msg_1", "order_1", 4)

    assert receipt["action_id"] == "act_1"
    assert len(seen) == 2
    assert seen[0]["idempotency_key"] == seen[1]["idempotency_key"]
    assert seen[0]["type"] == "order.cancel"
    assert seen[0]["payload"]["reason"] == "customer_request"


def test_deterministic_key_survives_later_call_after_uncertain_result() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(504, json={"detail": "committed, response lost"})
        return httpx.Response(200, json={"action_id": "act_1"})

    with Client(
        "http://aiday.test", "token", transport=httpx.MockTransport(handler),
        max_retries=0, backoff_base=0,
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.issue_upsert("msg_1", {"issue_id": "i1", "title": "Status"})
        client.issue_upsert("msg_1", {"issue_id": "i1", "title": "Status"})

    assert len(seen) == 2
    assert seen[0]["idempotency_key"] == seen[1]["idempotency_key"]


def test_network_error_retries_and_keeps_key() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"action_id": "act_2"})

    with Client(
        "http://aiday.test", "token", transport=httpx.MockTransport(handler),
        max_retries=1, backoff_base=0, backoff_max=0,
    ) as client:
        assert client.reply("msg_2", {"template_id": "clarification"})["action_id"] == "act_2"

    assert len(seen) == 2
    assert seen[0]["idempotency_key"] == seen[1]["idempotency_key"]


def test_conflict_is_not_retried() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, json={"code": "stale_version"})

    with Client(
        "http://aiday.test", "token", transport=httpx.MockTransport(handler),
        max_retries=4, backoff_base=0,
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.cancel_order("msg_1", "order_1", 1)

    assert calls == 1


def test_read_methods_use_contract_paths_and_auth() -> None:
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Authorization")))
        return httpx.Response(200, json={"messages": []})

    with Client("http://aiday.test", "secret", transport=httpx.MockTransport(handler)) as client:
        client.inbox()
        client.query("msg_1", "SELECT 1", {})

    assert calls == [
        ("GET", "/v1/inbox", "Bearer secret"),
        ("POST", "/v1/query", "Bearer secret"),
    ]
