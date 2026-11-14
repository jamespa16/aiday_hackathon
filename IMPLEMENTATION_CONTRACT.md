# Implementation coordination contract

This file coordinates parallel development. The implemented scope is an agent-assisted 90 minute event: three cumulative tiers, independent one-message episodes, seeded synthetic requests, scoped SQL reads, typed writes and deterministic rendered replies. Conversation branching and arbitrary prose judging remain future work.

Package: `aiday`. Python >=3.11. FastAPI server, sqlite3 persistence, httpx SDK, pytest tests. No paid runtime dependencies or model credentials required.

## Shared fixture format

`aiday.scenarios.generate_suite(seed=42, count=12)` returns JSON-serializable episode dictionaries:

```python
{
 'episode_id': 'ep_001', 'family': 'routine', 'now': '2026-11-14T18:00:00Z',
 'message': {'schema_version':'1.0','message_id':'msg_001','thread_id':'thread_001',
             'authenticated_customer_id':'customer_001','from_address':'alex@example.test',
             'subject':'...', 'body_text':'...', 'received_at':'...'},
 'initial_state': {'customers': [...], 'orders': [...], 'shipments': [...], 'refunds': [...]},
 'expected': {
   'disposition':'processed',
   'issues':[{'intent':'cancel_order','priority':'high','queue':'operations',
              'order_id':'order_001','extracted_fields':{}}],
   'operation': {'type':'order.cancel','order_id':'order_001','payload':{}} or None,
   'reply': {'template_id':'cancellation_confirmed','order_id':'order_001',
             'facts':{'refund_cents':4999},'requested_fields':[], 'policy_reason':None} or None,
   'workflow_status': { '1':'open', '2':'resolved', '3':'resolved' }
 }
}
```

All orders: order_id, order_number, customer_id, status, total_cents, captured_cents, refunded_cents, shipping_address (dict: line1, city, region, postal_code, country), created_at, delivered_at (nullable), version (int). Additional SQL flattened address columns can be projected. Customers: customer_id,name,email. Shipments: shipment_id,order_id,carrier,tracking_number,status,estimated_delivery_at. Refunds: refund_id,order_id,amount_cents,reason,created_at. Initial state contains another customer's order to verify isolation.

## Engine interface owned by Sol

`aiday.engine.new_run(episodes, tier=3, run_id=None)` makes JSON-serializable run state, deep copies each episode, and adds episode `state` (mutable copy of initial_state), `issues` (dict keyed by participant issue_id), `replies` (list of reply records), `events` (list), `completed` (bool), `disposition` (nullable).

Run: run_id, tier, episodes, episode_index, receipts (idempotency_key -> canonical request + receipt), events (list), finished (bool). Root may add storage metadata separately.

Engine functions: `active_episode(run)`, `apply_action(run, body) -> receipt`, `complete_message(run, message_id, body) -> receipt`, `query(run, body) -> {'columns': [...], 'rows': [dict,...]}`. Helpers can be added. Functions mutate passed run only as documented. `EngineError(status_code, code, message)` exposes those attributes. In case of rejection, retain audit events in run, but never retain partial business effects. Event shape: `{'sequence':int,'type':str,'message_id':str,'payload':dict, ...}`. Accepted actions also have `action_id`; query events include returned row identities or rows to prove evidence access. Error events include `code` and `prohibited` boolean. One sequence across the run.

Action envelope: schema_version='1.0', idempotency_key, message_id, type, payload. Types: issue.upsert, order.cancel, order.change_address, order.refund, reply.send. Strict schemas in engine or shared models. All order commands require order_id and expected_version; address change adds shipping_address; refund adds amount_cents and reason; cancel adds reason. All reasons='customer_request' for mutations. Use shared policy helpers to enforce eligibility. Issue payload follows SPEC.md (issue_id,thread_id,source_message_ids,title,intent,priority,queue,order_id,extracted_fields,status). Reply payload: in_reply_to,template_id,order_id(nullable),supporting_action_ids(default []),facts(default {}),requested_fields(default []),policy_reason(nullable). Reply record contains payload, action_id, rendered_body, state_at_send and sequence to grade truthfulness. Accept structurally valid semantic mistakes for grading; enforce ownership so private information cannot be disclosed.

Completion body: idempotency_key, disposition ('processed'|'ignored'). Cache completion idempotency separately or in same receipt namespace. Exact retries after advancement are accepted. Further writes to a completed episode rejected. Tier1 only issues; tier2 adds order writes; tier3 adds reply. Read queries all tiers. One reply per message.

`query` is an authorizer-protected SQLite SELECT against ephemeral customer-scoped tables, 200-row limit, 1s execution deadline; read only; named parameter bindings. Structure {'message_id':str,'sql':str,'parameters':dict}. No system tables, PRAGMA, extension loading, attachments or filesystem access. Emit evidence audit events. Querying another customer's order yields empty rows.

## Shared policy owned by root

`aiday.policy.POLICY` JSON dict and constants `INTENTS`, `QUEUES`, `PRIORITIES`, `STATUSES`. `remaining_balance(order)->int`, `can_cancel(order)->bool`, `can_change_address(order)->bool`, `can_refund(order,now)->bool`; refund deadline inclusive 30 days after delivery and not before delivery. Address keys line1,city,region,postal_code,country. Cancel/refund never duplicate payments.

Reply templates: order_status (facts status, optionally tracking_number, estimated_delivery_at), cancellation_confirmed (facts refund_cents), address_changed (facts shipping_address), refund_confirmed (facts refund_cents), policy_refusal (policy_reason required: already_shipped/refund_window_expired/already_refunded/not_eligible), clarification (requested_fields, e.g. order_number), escalation (policy_reason: ownership_unverified/damaged_item/incorrect_item/unsupported_request). Only known facts accepted, no arbitrary prose. Non-spam denied requests still create issue. Null order_id for unknown/inaccessible order.

## Grader interface owned by Terra

`aiday.grading.grade_run(run, detailed=True)->dict` returns schema_version, run_id, tier, score (0..100 rounded 2dp), components {'triage':0..1,'orders':0..1,'replies':0..1}, completed_episodes,total_episodes,fully_correct_episodes, prohibited_attempts, episodes (detailed per-episode reports if detailed). Weights as SPEC. Unfinished episodes receive zero (so silence does not earn points on unseen spam). Macro-average per family. No hidden fields in aggregate reports. Required effects compared to initial state and shared policy; immutable events and send-time state used to detect ordering and collateral writes. Issue matching deterministic max-weight assignment with extras penalized (cap submitted issues in engine to keep tractable). Expose `reference_actions(episode,tier)->list[dict]` if useful, but root owns the reference HTTP agent.

## HTTP API and persistence owned by root

`aiday.api.create_app(db_path='...', admin_token=None, practice=True)` builds app; Bearer run token on /v1 routes. Admin bearer token on `/admin/runs` POST (seed,count,tier,team_name) -> run_id,token; GET /admin/runs, GET /admin/runs/{id}, POST /admin/runs/{id}/close, GET /admin/runs/{id}/grade, GET /admin/runs/{id}/transcript; authenticated admin /admin/leaderboard. Practice-only POST /practice/runs creates runs and GET /v1/report returns detailed score. Evaluation mode disables these practice routes. Admin credentials never accepted as run credentials or exposed via public schemas. Local CLI prints practice token. Public GET /health, /v1/policy and /v1/schema; GET /v1/run returns run metadata only; GET /v1/inbox returns {'messages':[active message] or [],'next_cursor':opaque,'finished':bool}; GET /v1/issues -> {'issues':list}; POST /v1/query; POST /v1/actions; POST /v1/messages/{message_id}/complete; GET /v1/actions/{action_id}. No fixture expectations or seed in public output.

## SDK and starter owned by Luna

`aiday.client.Client(base_url, token, timeout=30)` sync httpx; run(), inbox(cursor=None), policy(), schema(), query(message_id,sql,parameters=None), actions helpers issue_upsert(message_id,issue, key=None), cancel_order(message_id,order_id,expected_version,key=None), change_address(...,shipping_address,...), refund_order(...,amount_cents,...), reply(message_id, payload, key=None), complete(message_id, disposition='processed',key=None), report(), close()/context manager. Retry transient/network errors with same key, do not blindly retry conflicts. `aiday.client.create_practice_run(base_url, tier=3, seed=42,count=12)->dict`. Provide `starter/agent.py` with three TODO functions and working minimal baseline, `starter/AGENTS.md`, examples and quickstart docs. CLI commands owned root (`aiday serve`, `aiday practice`, `aiday grade`, `aiday reference`, `aiday export-schemas`). README may document planned commands as implementation agreed here; coordinate deviations.

## File ownership

Sol: aiday/engine.py, aiday/sql.py, aiday/models.py, tests/test_engine.py, tests/test_sql.py. Terra: aiday/scenarios.py, aiday/grading.py, tests/test_scenarios.py, tests/test_grading.py. Luna: aiday/client.py, starter/, examples/, docs/PARTICIPANT.md, tests/test_client.py. Root: packaging, shared policy, API, persistence, CLI, organizer runner, README, integration tests, overall reconciliation. Do not overwrite other owners' files; communicate contract changes promptly.
