"""Small synchronous SDK for the AI Day practice and evaluation API."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx


class Client:
    """Authenticated API client.

    Transient network failures, 429s, and 5xx responses are retried with a
    bounded exponential delay. Writes always reuse a deterministic key for an
    identical request, including when a caller retries after an uncertain
    timeout. Conflicts (409) and other 4xx responses are returned immediately.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.1,
        backoff_max: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(0.0, float(backoff_max))
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError:
                if attempt >= self.max_retries:
                    raise
                self._sleep(attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.max_retries:
                    self._sleep(attempt, response.headers.get("Retry-After"))
                    continue
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
        raise RuntimeError("request retry loop ended unexpectedly")

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(self.backoff_max, self.backoff_base * (2**attempt))
        if retry_after is not None:
            try:
                delay = min(self.backoff_max, max(delay, float(retry_after)))
            except ValueError:
                pass
        if delay:
            time.sleep(delay)

    @staticmethod
    def _key(kind: str, message_id: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"kind": kind, "message_id": message_id, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return f"sdk-{kind}-{digest}"

    def _write(self, message_id: str, action_type: str, payload: dict[str, Any], key: str | None) -> dict[str, Any]:
        idempotency_key = key or self._key(action_type, message_id, payload)
        return self._request(
            "POST",
            "/v1/actions",
            json={
                "schema_version": "1.0",
                "idempotency_key": idempotency_key,
                "message_id": message_id,
                "type": action_type,
                "payload": payload,
            },
        )

    def run(self) -> dict[str, Any]:
        return self._request("GET", "/v1/run")

    def inbox(self, cursor: str | None = None) -> dict[str, Any]:
        params = {"cursor": cursor} if cursor is not None else None
        return self._request("GET", "/v1/inbox", params=params)

    def policy(self) -> dict[str, Any]:
        return self._request("GET", "/v1/policy")

    def schema(self) -> dict[str, Any]:
        return self._request("GET", "/v1/schema")

    def query(self, message_id: str, sql: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/query",
            json={"message_id": message_id, "sql": sql, "parameters": parameters or {}},
        )

    def issue_upsert(self, message_id: str, issue: dict[str, Any], key: str | None = None) -> dict[str, Any]:
        return self._write(message_id, "issue.upsert", issue, key)

    def cancel_order(
        self, message_id: str, order_id: str, expected_version: int, key: str | None = None
    ) -> dict[str, Any]:
        return self._write(
            message_id,
            "order.cancel",
            {"order_id": order_id, "expected_version": expected_version, "reason": "customer_request"},
            key,
        )

    def change_address(
        self,
        message_id: str,
        order_id: str,
        expected_version: int,
        shipping_address: dict[str, str],
        key: str | None = None,
    ) -> dict[str, Any]:
        return self._write(
            message_id,
            "order.change_address",
            {
                "order_id": order_id,
                "expected_version": expected_version,
                "shipping_address": shipping_address,
                "reason": "customer_request",
            },
            key,
        )

    def refund_order(
        self,
        message_id: str,
        order_id: str,
        expected_version: int,
        amount_cents: int,
        key: str | None = None,
    ) -> dict[str, Any]:
        return self._write(
            message_id,
            "order.refund",
            {
                "order_id": order_id,
                "expected_version": expected_version,
                "amount_cents": amount_cents,
                "reason": "customer_request",
            },
            key,
        )

    def reply(self, message_id: str, payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
        return self._write(message_id, "reply.send", payload, key)

    def complete(
        self, message_id: str, disposition: str = "processed", key: str | None = None
    ) -> dict[str, Any]:
        body = {"disposition": disposition}
        idempotency_key = key or self._key("complete", message_id, body)
        return self._request(
            "POST",
            f"/v1/messages/{message_id}/complete",
            json={"idempotency_key": idempotency_key, **body},
        )

    def report(self) -> dict[str, Any]:
        return self._request("GET", "/v1/report")


def create_practice_run(
    base_url: str, tier: int = 3, seed: int = 42, count: int | None = 12, *, round_type: str = "correctness"
) -> dict[str, Any]:
    """Create a local practice run and return the server's run/token payload."""
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30) as http:
        response = http.post("/practice/runs", json={"tier": tier, "seed": seed, "count": count, "round_type": round_type})
        response.raise_for_status()
        return response.json()
