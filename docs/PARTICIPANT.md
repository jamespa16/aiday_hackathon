# Participant quickstart

The challenge runs against synthetic customer support requests. Your agent
reads one active message, looks up only the authenticated customer's records,
submits structured issue and order actions, optionally sends a structured
reply, and completes the message. The 90-minute event uses independent
one-message episodes across three cumulative tiers.

## Start a model-free practice run

From the repository root, install the locked dependencies and start the server in one terminal:

```bash
uv sync --frozen --dev
uv run aiday serve
```

Create a Tier 1 practice run in another terminal; the command prints the run
token:

```bash
uv run aiday practice --tier 1
export AIDAY_BASE_URL=http://127.0.0.1:8000
export AIDAY_TOKEN='paste-the-practice-token'
uv run python -m starter.agent --tier 1
uv run aiday grade
```

The baseline needs Python and the project dependencies only. A model is
optional: teams can call their preferred model inside the three hooks, but
the smoke path makes no external requests and needs no credentials.

## Participant hooks

Edit [starter/agent.py](../starter/agent.py). The starter intentionally handles
obvious status-only messages and ignores other messages; it exists to confirm
that setup, authentication, the inbox, and completion work. These are the
three extension points for your solution:

1. `triage_message(message, client)` returns zero or more issue dictionaries.
2. `handle_order(message, client)` queries records and submits an eligible
   cancellation, address change, or refund.
3. `compose_reply(message, client)` returns the structured `reply.send` payload
   or `None` when the correct action is silence.

The main loop upserts returned issues, calls the order and reply hooks, and
completes the message. Keep the hook decisions grounded in the message,
published policy, schema, and query results. Do not infer private fixture
answers. See [starter/AGENTS.md](../starter/AGENTS.md) for fast local feedback.

## SDK and API

Use `aiday.client.Client(base_url, token)` for synchronous requests. It exposes
`run()`, `inbox()`, `policy()`, `schema()`, `query()`, `issue_upsert()`,
`cancel_order()`, `change_address()`, `refund_order()`, `reply()`, `complete()`,
and `report()`. The write helpers generate deterministic idempotency keys for
identical requests. They retry network errors, 429 responses, and server errors
with bounded backoff; a 409 conflict is returned so your agent can fetch fresh
state and decide again.

Queries are read-only SQLite `SELECT`s over customer-scoped tables. Use named
parameters, for example:

```python
rows = client.query(
    message["message_id"],
    "SELECT order_id, status, version FROM orders WHERE order_number = :number",
    {"number": "ORD-1042"},
)
```

Inspect the response's `columns` and `rows`; an empty result may mean the order
is unknown or outside the authenticated customer's scope. Use the returned
`version` as `expected_version` for an order action.

## Valid structured action example

The request transcript in
[examples/cancel_and_reply.json](../examples/cancel_and_reply.json) shows the
query, a permitted cancellation, a confirmation grounded in the successful
action, and message completion. Its `response_pointer` fields identify useful
response values using JSONPath-style pointers. Replace illustrative IDs and
facts with values returned by your own query and action receipt. The standalone
[valid issue payload](../examples/valid_cancellation_issue.json) shows the
machine-readable issue shape.

Use `customer_request` as the reason for order mutations. Only submit a
success reply after the referenced operation is accepted. Structured replies
are rendered by the service; no email is actually sent. For unsupported,
ambiguous, or unsafe requests, use the published escalation or clarification
templates and reasons rather than guessing.

## Measure throughput

Use `uv run aiday practice --throughput --tier 3` to create a timed volume run,
export its printed token, and run your agent as usual. `uv run aiday grade`
includes server timing, correct messages/minute, accuracy, and p50/p95 latency.
The default is 240 messages and up to 120 seconds. The ordinary API rate cap is
lifted in this round. Practice results cannot qualify for prize tie breaking.

Final throughput results require a perfect correctness run followed by three
eligible speed trials. All completed requests must be correct; early exits and
unfinished order/reply effects invalidate a speed trial. See
[the performance rules](PERFORMANCE.md) before optimizing.
