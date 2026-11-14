# Participant agent instructions

This is a small, model-free starter for the AI Day 90-minute challenge. Keep the
main loop unattended: read the active inbox message, make only decisions based
on the public message, policy, schema, and scoped query results, submit typed
actions, then complete the message.

The three extension hooks are in `agent.py`:

- `triage_message(message, client)` returns issues to upsert.
- `handle_order(message, client)` performs eligible order work or returns
  without changing state.
- `compose_reply(message, client)` returns a structured reply payload or
  `None` to send no reply.

The agent sets `client.tier` from the authenticated run metadata before calling
the hooks. Use it to branch on available capabilities; `client.run()` also
returns the public run limits and progress fields.

The default behavior intentionally handles status-only inquiries. It is a
working smoke baseline, not a competitive solution. Extend these three hooks;
do not make decisions depend on fixture files, private state, seeds, or grader internals. Query data
through `client.query()` with `message_id` and named SQL parameters.

Fast feedback from the repository root:

```bash
uv run aiday practice --tier 1
uv run python -m starter.agent --help
uv run pytest -q tests/test_client.py
```

If the practice command prints a run token, export it as `AIDAY_TOKEN` and run
the agent in another terminal. Set `AIDAY_BASE_URL` if the service is not at
`http://127.0.0.1:8000`. No model package, model server, or model credentials
are needed for the baseline.

Read `CHALLENGE.md` for the competition rules. Practice score reports are
allowed development feedback: use `uv run aiday grade` after running the agent.
Implement decisions from inbox messages and scoped API data; do not make the
solution depend on practice seeds, grader internals, or expected-answer files.
