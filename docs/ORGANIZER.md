# Organizer runbook

## Rehearse the 90 minute event

Give a volunteer the participant starter, `CHALLENGE.md`, and a coding agent. Confirm they can run the baseline and see a score in five minutes. The shipped baseline recognizes a few status messages and deliberately earns only partial credit. The engine, transport, and grader require no model.

Allow approximately 5 minutes for setup, 60 minutes for building, 15 minutes for public test feedback, and 10 minutes for submission. Prebuild the example image before the event; final evaluation can run after the 90 minute build window. Freeze package versions, rules, prize eligibility, shared-first-place handling, and the submission deadline before starting. The code supports one final run per team per tier and shared ranks; organizers decide how the $500 is allocated.

The implementation uses independent one-message episodes, unlike the original broader draft in `SPEC.md`. Twelve types cover order status, cancellation, address change, eligible refund, shipped cancellation, the inclusive refund deadline, an expired refund, an already refunded order, spam, missing order reference, inaccessible order reference, and malicious message instructions. Every block of twelve covers these types. The default final suite has 24 episodes, which keeps coverage balanced.

## Public practice

```bash
uv sync --frozen --dev
uv run aiday serve
```

Teams can create their own practice runs using `aiday practice`. The local `/v1/report` exposes detailed expected answers and observations. It intentionally does so for public practice only. Use `aiday export-schemas` to publish machine-readable policy, SQL columns, and action schemas. API docs are at `/docs`.

Verify the release while the practice server is running:

```bash
uv run pytest
uv run aiday reference --tier 1 --count 24 --output reports/reference-tier1.json
uv run aiday reference --tier 2 --count 24 --output reports/reference-tier2.json
uv run aiday reference --tier 3 --count 24 --output reports/reference-tier3.json
```

Each reference report should score 100. Reference actions use public scenario expectations to test infrastructure; participants should solve from inbox and API observations. The grader includes partial credit, so correctly ignoring spam and preserving state can earn some points even in a weak submission. A score is not a percentage of fully solved emails; publish `fully_correct_episodes` alongside it.

## Start final evaluation

Use a separate private database. Create a random organizer credential in your terminal without putting it in repository files:

```bash
export AIDAY_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run aiday serve --evaluation --db .aiday/final.sqlite3 --suite-count 24
```

Keep the credential to reuse after a restart. The service generates a secret 128-bit suite seed on first start, saves the exact fixtures and policy in the database, and fingerprints the grading/domain code. Every team receives the same fixtures. The seed and expectations never appear in participant routes. A restart reuses the saved suite. Changing policy or grading/domain code is rejected against an already frozen database. Retain the frozen source release, lockfile, and private database as one evaluation record.

Evaluation mode removes `/practice/runs` and `/v1/report`. Only organizer credentials can create runs, view grades, close runs, download transcripts, and inspect the leaderboard. Run tokens cannot call organizer routes; organizer tokens cannot impersonate run tokens. Store only token hashes in the database; creation returns the participant token once. Run one server worker so the in-memory rate limiter has one authoritative bucket per run.

Final run creation accepts `team_name` and `tier`; omit `seed` and `count` because the suite is frozen. A database uniqueness constraint rejects another run for the same normalized team name and tier, including concurrent creation. Teams submit one image that can run at any tier through `AIDAY_TIER`; you can evaluate the declared tier or all three according to the rules you announce.

## Docker deployment and submissions

The event server should run separately from submitted programs. The supplied runner uses a dedicated Docker bridge network, mounts no host directories, drops capabilities, uses a non-root user, makes the application filesystem read-only, and limits CPU, memory, process count, and logs. Use an organizer machine dedicated to evaluation. Docker resource isolation is not a complete sandbox for hostile native exploits; the competition policy must prohibit attempts to attack the host or other submissions.

Start the server container with a private persistent volume and the configured organizer token:

```bash
docker compose up --build -d
```

It publishes port 8000 on localhost for organizer commands and is reachable as `aiday-server` on the `aiday-event` network. The container starts in evaluation mode. Use `docker compose logs server` for server diagnostics. Keep the volume; deleting it removes the frozen suite and run history.

Build the participant example after editing its hooks:

```bash
docker build -f starter.Dockerfile -t team-example:final .
uv run aiday evaluate --image team-example:final --team "Example Team" --tier 3
```

The runner requires an already built local image; it does not build or pull during the timed run. Entrypoints receive `AIDAY_BASE_URL`, `AIDAY_TOKEN`, and `AIDAY_TIER` and must run without prompts. Install all required dependencies in advance, make application files readable by UID 65532, and write temporary data only under `/tmp`. Include model libraries and settings in the team's image as appropriate; the example requires only the HTTP SDK.

By default the runner forwards no model credentials. Choose a common model allowance or a bring-your-own-model policy before the event. If organizers provide an event-scoped model credential, explicitly forward selected settings:

```bash
uv run aiday evaluate --image team-example:final --team "Example Team" --tier 3 \
  --model-env OPENAI_API_KEY --model-env OPENAI_MODEL
```

This software limits event-server requests and execution resources, not provider token spending. Use provider-side allowances or an event model gateway if you need a shared monetary cap. The bridge network permits outbound provider access. Do not forward organizer API credentials or an unrestricted organization model key to submissions.

## Results and disputes

Each `evaluate` invocation saves a summary, bounded container logs, and a full private transcript under `reports/`. Files use owner-only permissions. A crash, nonzero exit, or timeout closes the run and grades completed episodes; the execution result is recorded separately. Review infrastructure failures before accepting the result. The runner does not automatically grant another attempt.

```bash
uv run aiday leaderboard --output reports/leaderboard.json
uv run aiday transcript RUN_ID --output reports/transcript.json
uv run aiday replay reports/transcript.json --output reports/replayed.json
```

Replay recomputes the deterministic score from saved state and immutable action history. Transcripts contain private fixtures, other-customer records, the suite seed, and expectations; share summaries during competition and detailed feedback only after the deadline under your announced dispute rules. Raw transcripts are organizer evidence, rather than a team-facing report.

The leaderboard sorts within a tier by score and fully correct episodes, then uses eligible three-trial median throughput to break perfect-score ties, followed by prohibited attempts and accepted write count. Equal values share rank. The optional performance round requires a perfect correctness run and is documented in [PERFORMANCE.md](PERFORMANCE.md). It does not assign prizes or enforce an award eligibility threshold. Mark infrastructure failures separately, preserve their transcripts, and announce any rerun rule consistently; a replacement evaluation database creates a new frozen suite for all affected teams.

## Deployment verification

The automated tests exercise the API, transaction semantics, SQL isolation, grading, retries, restart persistence, frozen suites, duplicate creation races, and tied leaderboard ranks. `scripts/smoke_docker.py` additionally runs the actual example submission through a disposable server and network:

```bash
docker build -t aiday-event:local .
docker build -f starter.Dockerfile -t aiday-starter:local .
uv run python scripts/smoke_docker.py
```

Rehearse with a volunteer's agent-generated solution before event day. The current scenario generator varies IDs, versions, amounts, names, addresses, and selected phrasing; the case families and business policy are intentionally public. It is a short tool-use competition rather than a comprehensive natural-language understanding benchmark. SQL audit metadata verifies relevant column access but does not prove complete expression-level data provenance.
