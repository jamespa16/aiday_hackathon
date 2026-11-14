# Correctness gated throughput round

Use this optional round to distinguish teams that solve every correctness test. Correctness points remain the primary ranking. Speed can break a tie between perfect submissions after all three performance trials are eligible.

## Workload and timing

Default profile: 240 independent one-message episodes, up to 120 seconds of processing, three trials. Each workload contains twenty complete blocks of the same twelve public scenario types. Trial workloads use separate hidden random seeds, are frozen in the evaluation database, and are identical for every team at the same trial number. No new policy or intent is introduced by the speed round.

Timing is owned by the server. The clock starts with the first authenticated work request, including the starter's `/v1/run` call. Container startup, imports, and any preparation before that request are excluded. Completion is measured at admission of a successful final completion request; earlier work, model latency, polling, network round trips, and server overhead are included. Exact retries never reset timestamps or add completed work. Organizer polling and reports do not start a run's clock.

If all 240 requests finish, the denominator is time to the final completion. Otherwise, the denominator is the full 120-second window. Stopping or crashing with unfinished work before the window expires cannot produce an eligible speed score. If no work starts, no valid rate is reported. The runner also has an overall watchdog, including startup; leave its default 900 seconds or choose a value longer than the processing window plus startup allowance.

The regular 10-request-per-second throttle is disabled for throughput runs so it does not set an artificial ceiling. Other protections remain: strict schemas, customer scope, transactional effects, SQL limits, container resource limits, and a request budget of at least 30 API requests per episode (7,200 at the default profile). The current server releases one active episode at a time. This measures sequential end-to-end agent throughput rather than parallel queue or batch processing.

## Metrics and eligibility

Reports include:

| Field | Meaning |
| --- | --- |
| `correct_per_minute` | Fully correct completed episodes divided by measured seconds, times 60 |
| `completed_per_minute` | All completed episodes divided by measured seconds, times 60 |
| `accuracy` | Fully correct completed episodes divided by completed episodes |
| `latency_p50_seconds` | Median activation-to-completion latency of completed episodes |
| `latency_p95_seconds` | Interpolated 95th percentile of activation-to-completion latency |
| `api_requests` | Counted API requests consumed by the run |
| `server_handler_seconds` | Diagnostic handler time; excludes the final storage commit/serialization and is not subtracted from the throughput clock |
| `workload_exhausted` | Whether the agent finished the full workload before the deadline |
| `eligible_for_tiebreak` | Whether qualification, timing, and quality gates all pass |

For final evaluation, first earn a perfect correctness result for the same team and tier. The check uses the number of fully correct episodes, not a rounded score that happens to display 100. Each speed trial must have 100% accuracy among completed episodes, zero prohibited attempts, and no unfinished request with an accepted order mutation or outbound reply. An issue draft without a business effect at cutoff is allowed and receives no throughput credit. A failed quality gate preserves the diagnostic metrics but makes the result ineligible.

Correctly ignoring spam counts as correct handling because it is part of the fixed workload. The queue order is identical for all teams, and an agent cannot advance past a difficult request without completing it. Completing a difficult request incorrectly invalidates the quality gate. These rules make it unhelpful to skip hard cases or submit a short easy prefix and quit.

For example, 240 correct requests in 80 seconds is 180 correct requests/minute. Finishing 180 correct requests by the 120-second deadline is 90/minute. Finishing 180 requests in 60 seconds and quitting with 60 still queued is ineligible. Finishing 240 quickly with one incorrect result is also ineligible.

Use the median rate of all three eligible trials, rounded to three decimals, as the tie breaker. One fast outlier does not decide the prize. Missing, repeated, or failed trials cannot generate a median tie-break result. Correctness runs and speed trials occupy separate entry slots; each team can run each speed trial once. The leaderboard attaches the three-trial summary to the qualifying correctness entry and never mixes speed-trial correctness percentages into the main leaderboard.

## Commands

Practice uses the normal local service and the same starter:

```bash
uv run aiday serve
# Another terminal:
uv run aiday practice --throughput --tier 3
# Export the printed AIDAY_BASE_URL and AIDAY_TOKEN first.
uv run python -m starter.agent --tier 3
uv run aiday grade
```

To calibrate infrastructure with the public reference solver:

```bash
uv run aiday reference --throughput --tier 3 --output reports/reference-speed.json
```

The reference solver has direct public fixture expectations and makes no model calls, so it measures the infrastructure floor rather than a competitive participant result. Calibrate on the actual event machine before freezing the profile. A faster server, a larger workload, or both may be needed if several solutions approach that floor. Run teams sequentially on the same machine, with identical CPU/memory limits, stable model access, and the same announced model allowance. This software measures provider latency but does not control provider congestion or spending.

Start a final server with a frozen profile (a separate database from practice):

```bash
uv run aiday serve --evaluation --db .aiday/final.sqlite3 \
  --benchmark-count 240 --benchmark-seconds 120
```

After a team's correctness run is perfect:

```bash
uv run aiday benchmark --image team-example:final --team "Example Team" --tier 3 \
  --qualifying-run RUN_ID
uv run aiday leaderboard
```

The benchmark command launches three fresh containers using the same image and isolation settings as correctness evaluation. The runner resolves the image to an immutable image ID before launching it, and the API requires that ID to match the qualifying run; retagging or changing an image cannot switch implementations between rounds. Provider settings can be explicitly forwarded using the same `--model-env` options as `evaluate`. It saves each transcript and a combined median summary. Announce whether this round will determine ties before teams start, and preserve the same model configuration and allowance between correctness and speed evaluation.

Profile count must be a multiple of twelve, from 12 to 1,200; duration must be 1–900 seconds. Once an evaluation database is frozen, changing the profile or grading/timing/domain code requires the frozen release or a new database. Existing frozen databases from an older release are intentionally rejected after the timing-rules upgrade; use a fresh rehearsal database.
