# AI Day Support Automation Challenge

A Python event server, seeded inbox simulator, participant SDK, and deterministic autograder for an agent-assisted 90 minute hackathon. Three cumulative tiers cover inbox triage, order operations, and automatic replies.

## Quick start

```bash
uv sync --frozen --dev
uv run aiday serve
```

In another terminal, create a practice run and use the printed configuration:

```bash
uv run aiday practice --tier 3 --seed 42 --count 12
```

Copy the two printed `export` commands into your terminal, then run:

```bash
uv run python -m starter.agent --tier 3
uv run aiday grade
```

The baseline needs no API key. It is deliberately incomplete: teams implement `triage_message`, `handle_order`, and `compose_reply` in [starter/agent.py](starter/agent.py). Start with [the participant guide](docs/PARTICIPANT.md) and [agent instructions](starter/AGENTS.md). Interactive HTTP docs are at [localhost:8000/docs](http://127.0.0.1:8000/docs).

## What is implemented

- Three cumulative tiers with typed, validated issue, order, and reply actions.
- Twelve scenario types with deterministic seeded variations, including eligible operations, policy boundaries, unknown/foreign orders, spam, and malicious message instructions.
- Customer-scoped read-only SQLite queries; transactional cancellation/refund/address commands; order versions; idempotent retries.
- Structured replies rendered into a simulated outbox with immutable send-time evidence.
- Persistent SQLite runs, separate organizer/run credentials, request/rate/time budgets, and hidden evaluation suites frozen across restarts.
- Deterministic partial-credit grading, per-family averages, field-level deductions, shared leaderboard ranks, and replayable JSON transcripts.
- A synchronous HTTP SDK, a runnable participant starter, and a resource-limited Docker evaluation runner.
- An optional correctness-gated volume round with three-trial median throughput, server timing, p50/p95 latency, and perfect-score leaderboard tie breaking.

The shipped scope is independent one-message episodes. Multi-message conversation branching, arbitrary SQL writes, real email, attachments, and model-judged freeform prose are deferred. [SPEC.md](SPEC.md) preserves the original broader proposal; [CHALLENGE.md](CHALLENGE.md), the exported policy, and the implemented API define this release.

## Verify the system

```bash
uv run pytest
uv run aiday reference --tier 1 --output reports/tier1.json
uv run aiday reference --tier 2 --output reports/tier2.json
uv run aiday reference --tier 3 --output reports/tier3.json
uv run aiday export-schemas
```

The reference commands exercise the real HTTP service and should each score 100. They are practice tools using known public fixtures, rather than participant solutions. An empty/no-op submission receives substantially less credit; grader regression tests also cover extra tasks, false confirmations, excess refunds, and incorrect operations followed by repair.

For speed testing, use `uv run aiday practice --throughput --tier 3`, then run the same participant agent and inspect `aiday grade`. This uses a larger timed workload without the ordinary API rate cap. [Performance round instructions](docs/PERFORMANCE.md) explain final evaluation and the correctness guard.

## Run the event

Read [the organizer runbook](docs/ORGANIZER.md) for final evaluation, Docker submissions, model credentials, score publication, and a 90-minute rehearsal checklist. Evaluation mode requires a separate database and organizer token, disables practice endpoints, and freezes one private suite shared by all teams. Keep that database and transcripts private.

The release uses Python 3.11+; development and Docker are tested with Python 3.13. Dependencies are locked in `uv.lock` and the server image installs the corresponding hashed runtime export. No external model is needed by the simulator or grader.
