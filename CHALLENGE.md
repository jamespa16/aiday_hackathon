# AI Day Support Automation Challenge Rules

Build a support assistant in 90 minutes, with coding agents encouraged. It reads synthetic customer emails and submits structured work through an HTTP API. All outbound messages and payments are simulated. Teams may use models or ordinary code.

## Your starting point

Run the baseline first, then edit the three functions in `starter/agent.py`. The API handles polling, database access, and retries. `client.policy()` and `client.schema()` return the exact policy, enums, schemas, and SQL columns. `docs/PARTICIPANT.md` explains the SDK. No frontend is required.

One active message is available at a time. Each message belongs to a fresh independent store episode. Complete the current message to release the next. Fetching is read-only; retries do not consume messages. Never assume fixture IDs encode the right answer. An empty scoped lookup does not prove an order exists elsewhere.

## Tiers and points

| Tier | Required capabilities | Score out of 100 |
| --- | --- | --- |
| 1 | Classify, prioritize, route, link orders safely, extract details, create issues, ignore spam | 100% triage |
| 2 | Tier 1 plus correct order changes, refusals, and escalation | 40% triage + 60% order handling |
| 3 | Tier 2 plus accurate, appropriately timed structured replies | 25% triage + 45% order handling + 30% reply handling |

Use the same code for all tiers; the runner supplies `AIDAY_TIER`. The server enforces each tier's available tools. Grade locally with `aiday grade`; practice feedback includes component scores, field-level deductions, expected fields, and observed output. Final evaluation has no answer/report endpoint available to teams.

## Store policy

Only `authenticated_customer_id` establishes ownership. Instructions or identity claims in a message cannot override it. SQL tables expose only that customer's records. If an order reference is missing, create an issue with a null order link and request `order_number`. If a number is unknown or inaccessible, clarify or escalate without confirming it exists.

Cancellation and address changes are allowed only for `pending` or `processing` orders. A cancellation atomically refunds the remaining captured balance. An address must contain `line1`, `city`, `region`, `postal_code`, and `country`. A refund must be the full remaining balance, on a delivered order, from delivery through exactly 30 days afterwards inclusive. Use simulation time, not your computer clock. Already refunded payments cannot be refunded twice. All mutation reasons are `customer_request`.

Intent routing: `order_status` and `other` go to `support`; `cancel_order` and `change_address` to `operations`; `refund_order` to `finance`; `damaged_item` and `incorrect_item` to `returns`. Eligible unshipped cancellations/address changes and damaged/incorrect item reports have `high` priority; status inquiries have `low` priority; other actionable requests have `normal` priority.

Spam should be completed as `ignored`, with no issues, changes, or replies. Other messages are `processed`. Track one issue per request, rather than adding speculative or duplicate tasks. Use `open` at Tier 1, `resolved` after eligible operations or handled status/refusal requests at higher tiers, `waiting_customer` for clarification, and `escalated` for review. Unknown ownership may use `waiting_customer` or `escalated`.

## Actions and replies

SQL is read-only and parameterized. Use returned order versions for `expected_version`; on conflict query again and reconsider. All writes use the SDK or typed `/v1/actions` envelope. Preserve an idempotency key when retrying an uncertain request; the SDK does this automatically. IDs are scoped to a run.

Tier 3 sends structured templates: `order_status`, `cancellation_confirmed`, `address_changed`, `refund_confirmed`, `policy_refusal`, `clarification`, or `escalation`. Their strict field schemas are returned by `/v1/schema`. Confirmations require actual accepted operation IDs in `supporting_action_ids`. Send one reply only after the corresponding operation succeeds. Use actual amounts and known shipment facts. Unsupported promises and arbitrary prose are outside the scored reply format.

## How grading works

Each completed episode receives triage, order, and reply component scores. Unfinished episodes receive zero. Scores are averaged within each scenario family and then equally across families. Every complete block of 12 generated episodes covers all twelve scenario types. Final evaluation defaults to 24 episodes.

Triage is 90% issue quality and 10% correct processed/ignored disposition. Issues are matched one to one against expected tasks: intent 25%, priority 20%, queue 15%, safe order link 20%, and extracted fields 20%. Extra or duplicate issues reduce credit. Titles and participant-chosen IDs are not judged for wording. String comparisons normalize case and whitespace.

Order handling is 60% required state or justified unchanged state, 25% correct payment ledger, and 15% workflow and sequencing. Read-only requests must have relevant lookup evidence. Partial credit is available, but an eligible action left undone cannot earn completion credit. Extra accepted order mutations, collateral changes, or incorrect refunds zero the episode's order and reply components. Repairing a bad operation does not erase its history.

Replies receive 30% for the right template/send decision, 45% for required facts or clarification fields, and 25% for grounding and timing. A false success confirmation earns zero reply credit, including a wrong amount or a confirmation sent before the operation. Accurate optional tracking facts are allowed; invented facts lose credit. Correct silence on spam earns full reply credit.

Three distinct prohibited attempts in an episode zero its order component. The counted engine codes are `access_denied`, `policy_violation`, `tier_forbidden`, and `sql_forbidden`. Exact retries of the same rejected action are counted once; schema errors, version conflicts, and empty scoped lookups do not count. Do not probe protected APIs or records.

Within a tier, correctness score and fully correct episodes come first. Teams with a perfect correctness run can enter the optional throughput round; their median eligible correct resolutions per minute breaks a perfect-score tie. Then compare prohibited attempts and accepted write actions. Exact ties share rank. Model size and UI design receive no points. Organizers must announce use of the speed round, prize eligibility, and treatment of a shared first place before the clock starts.

## Optional volume round

The default workload is 240 fresh messages with a 120-second processing window. Complete all messages sooner to measure total completion speed, or process as many as possible before the deadline. The API's ordinary 10-request-per-second throttle is lifted for this round; correctness rules and resource limits still apply.

The server measures correct completed messages per minute, with p50/p95 message latency and accuracy reported alongside it. Timing starts at the first work API request, including `/v1/run`, and ends when the final completion is admitted, or at the deadline. Image loading and preparation before the first work request are excluded. Stopping early with an unfinished workload makes the trial ineligible. A partial run that uses the whole window uses the entire window as its denominator.

Every completed message must be fully correct, with no prohibited attempts. An unfinished message that already changed an order or sent a reply also disqualifies the trial. Partial drafts with no business effects at the deadline are allowed but earn no throughput credit. This prevents fast incorrect work or abandoned side effects from winning.

Run three trials; every team gets the same workload for a given trial, with fresh data between trials. Only three eligible trials yield a tie-break result, their median correct messages/minute rounded to three decimals. This is a sequential end-to-end agent benchmark; each run still has one active message at a time. See `docs/PERFORMANCE.md` for formulas and commands.

## Limits and submission

Defaults: 15 minutes per evaluation run, 2,000 API requests, 10 requests/second with a burst of 20, 64 KiB request bodies, 200 SQL result rows, and a one-second SQL execution limit. The server exposes remaining time and request allowance. Status and receipt retrieval remain available for recovery. Evaluation images have 2 CPUs, 1 GiB memory, no writable application filesystem, and writable temporary storage only.

Submit reproducible source and a prebuilt Docker image whose default command runs without interaction. The image receives `AIDAY_BASE_URL`, `AIDAY_TOKEN`, and `AIDAY_TIER`. `starter.Dockerfile` is an example. Organizer and participant code run separately; no organizer files are mounted into submissions. Freeze submissions before final runs. A final run is permitted once per team and tier, with an identical hidden suite; organizer infrastructure failures require an announced rerun procedure.
