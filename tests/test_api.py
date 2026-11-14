from __future__ import annotations

from typing import Any

import pytest
import httpx
from fastapi.testclient import TestClient

from aiday.api import create_app
from aiday.client import Client
from starter.agent import process_message


def _create_practice(client: TestClient, *, tier: int = 3) -> dict[str, Any]:
    response = client.post(
        "/practice/runs",
        json={"seed": 42, "count": 12, "tier": tier, "team_name": "boundary-test"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


@pytest.fixture
def practice_client():
    app = create_app(":memory:", admin_token="a" * 32, rate_limit=False)
    with TestClient(app) as client:
        yield client


def test_practice_create_and_public_metadata_are_scoped(practice_client: TestClient) -> None:
    created = _create_practice(practice_client)
    assert set(created) == {"run_id", "token", "tier", "total_episodes"}
    assert created["tier"] == 3
    assert created["total_episodes"] == 12

    response = practice_client.get("/v1/run", headers={"Authorization": f"Bearer {created['token']}"})
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["run_id"] == created["run_id"]
    assert metadata["tier"] == 3
    assert metadata["total_episodes"] == 12
    for forbidden in ("seed", "expected", "state", "initial_state", "episodes", "receipts"):
        assert not _contains_key(metadata, forbidden)


def test_bearer_is_required_and_admin_and_run_tokens_are_separate(practice_client: TestClient) -> None:
    created = _create_practice(practice_client)
    assert practice_client.get("/v1/run").status_code == 401
    assert practice_client.get("/v1/run", headers={"Authorization": "Bearer not-a-run-token"}).status_code == 401

    run_headers = {"Authorization": f"Bearer {created['token']}"}
    admin_headers = {"Authorization": f"Bearer {'a' * 32}"}
    assert practice_client.get("/admin/runs", headers=run_headers).status_code == 401
    assert practice_client.get("/v1/run", headers=admin_headers).status_code == 401


def test_scoped_sql_does_not_return_another_customers_order(practice_client: TestClient) -> None:
    created = _create_practice(practice_client)
    headers = {"Authorization": f"Bearer {created['token']}"}
    message = practice_client.get("/v1/inbox", headers=headers).json()["messages"][0]

    # Obtain a foreign order number from server-side test setup only. The HTTP
    # response must still return an empty, schema-shaped result to the client.
    run = practice_client.app.state.store.read(created["run_id"])
    active = run["episodes"][0]
    customer_id = message["authenticated_customer_id"]
    foreign = next(order for order in active["state"]["orders"] if order["customer_id"] != customer_id)
    result = practice_client.post(
        "/v1/query",
        headers=headers,
        json={
            "message_id": message["message_id"],
            "sql": "SELECT order_id, order_number, status FROM orders WHERE order_number = :number",
            "parameters": {"number": foreign["order_number"]},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["columns"] == ["order_id", "order_number", "status"]
    assert result.json()["rows"] == []


def test_completion_retry_returns_original_receipt_after_advancement(practice_client: TestClient) -> None:
    created = _create_practice(practice_client)
    headers = {"Authorization": f"Bearer {created['token']}"}
    message = practice_client.get("/v1/inbox", headers=headers).json()["messages"][0]
    body = {"idempotency_key": "complete-once", "disposition": "processed"}
    path = f"/v1/messages/{message['message_id']}/complete"

    first = practice_client.post(path, headers=headers, json=body)
    assert first.status_code == 200, first.text
    repeated = practice_client.post(path, headers=headers, json=body)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()
    assert repeated.json()["finished"] is False


def test_action_receipt_can_be_retrieved_by_action_id(practice_client: TestClient) -> None:
    created = _create_practice(practice_client, tier=1)
    headers = {"Authorization": f"Bearer {created['token']}"}
    message = practice_client.get("/v1/inbox", headers=headers).json()["messages"][0]
    action = {
        "schema_version": "1.0",
        "idempotency_key": "issue-once",
        "message_id": message["message_id"],
        "type": "issue.upsert",
        "payload": {
            "issue_id": "test-issue",
            "thread_id": message["thread_id"],
            "source_message_ids": [message["message_id"]],
            "title": "Order status inquiry",
            "intent": "order_status",
            "priority": "low",
            "queue": "support",
            "order_id": None,
            "extracted_fields": {},
            "status": "open",
        },
    }
    accepted = practice_client.post("/v1/actions", headers=headers, json=action)
    assert accepted.status_code == 200, accepted.text
    action_id = accepted.json()["action_id"]
    receipt = practice_client.get(f"/v1/actions/{action_id}", headers=headers)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json() == accepted.json()


def test_request_body_over_64_kib_is_rejected(practice_client: TestClient) -> None:
    created = _create_practice(practice_client)
    response = practice_client.post(
        "/v1/query",
        headers={"Authorization": f"Bearer {created['token']}", "Content-Type": "application/json"},
        content=b" " * (64 * 1024 + 1),
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_evaluation_mode_omits_practice_creation_and_report() -> None:
    app = create_app(":memory:", admin_token="e" * 32, practice=False, rate_limit=False)
    with TestClient(app) as client:
        assert client.post("/practice/runs", json={"seed": 42, "count": 12, "tier": 1}).status_code == 404
        assert client.get("/v1/report").status_code == 404


def test_evaluation_requires_at_least_24_character_admin_token() -> None:
    with pytest.raises(ValueError, match="at least 24 characters"):
        create_app(":memory:", admin_token="e" * 23, practice=False, rate_limit=False)

    app = create_app(":memory:", admin_token="e" * 24, practice=False, rate_limit=False)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["mode"] == "evaluation"


def test_model_free_starter_processes_an_api_message(practice_client: TestClient) -> None:
    created = _create_practice(practice_client, tier=1)

    def forward(request: httpx.Request) -> httpx.Response:
        response = practice_client.request(
            request.method,
            request.url.path + (f"?{request.url.query.decode()}" if request.url.query else ""),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content, request=request
        )

    with Client("http://aiday.test", created["token"], transport=httpx.MockTransport(forward)) as client:
        message = client.inbox()["messages"][0]
        process_message(message, client, tier=1)
        assert client.run()["completed_episodes"] == 1
