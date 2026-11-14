"""Local practice and organizer commands."""

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

import httpx

from .performance import BENCHMARK_COUNT, BENCHMARK_SECONDS, performance_report


def _json(value, output=None):
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        print(f"Saved {target}")
    else:
        print(text, end="")


def _organizer(args):
    if not args.admin_token:
        raise ValueError("Set AIDAY_ADMIN_TOKEN or pass --admin-token")
    return httpx.Client(base_url=args.base_url, headers={"Authorization": "Bearer " + args.admin_token}, timeout=30)


def _reference(args):
    """Exercise a public practice fixture through the actual HTTP boundary."""
    from .client import Client, create_practice_run
    from .grading import reference_actions
    from .scenarios import generate_suite
    round_type = "throughput" if args.throughput else "correctness"
    count = args.count if args.count is not None else BENCHMARK_COUNT if args.throughput else 12
    created = create_practice_run(args.base_url, tier=args.tier, seed=args.seed, count=count, round_type=round_type)
    episodes = generate_suite(seed=args.seed, count=count)
    with Client(args.base_url, created["token"]) as client:
        for episode in episodes:
            mid = episode["message"]["message_id"]
            page = client.inbox()
            if page["finished"]:
                break
            if not page["messages"] or page["messages"][0]["message_id"] != mid:
                raise ValueError("Server fixture differs from this release's practice generator")
            if episode["expected"]["disposition"] != "ignored":
                client.query(mid, "SELECT * FROM orders")
            operation_id = None
            for action in reference_actions(episode, args.tier):
                if action["type"] == "reply.send" and operation_id:
                    action["payload"]["supporting_action_ids"] = [operation_id]
                receipt = client._request("POST", "/v1/actions", json=action)
                if action["type"].startswith("order."):
                    operation_id = receipt["action_id"]
            client.complete(mid, episode["expected"]["disposition"])
        report = client.report()
    _json(report, args.output)
    return 0 if report["score"] == 100 else 1


def parser():
    result = argparse.ArgumentParser(description="AI Day event server and autograder")
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the local practice or organizer evaluation server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", default=".aiday/event.sqlite3")
    serve.add_argument("--evaluation", action="store_true", help="Disable all practice routes and freeze final fixtures")
    serve.add_argument("--admin-token", default=os.environ.get("AIDAY_ADMIN_TOKEN"))
    serve.add_argument("--request-limit", type=int, default=2000)
    serve.add_argument("--time-limit", type=int, default=900)
    serve.add_argument("--suite-seed", type=int, default=None, help="Organizer-only; random secret seed generated if omitted")
    serve.add_argument("--suite-count", type=int, default=24)
    serve.add_argument("--benchmark-count", type=int, default=BENCHMARK_COUNT)
    serve.add_argument("--benchmark-seconds", type=int, default=BENCHMARK_SECONDS)

    def remote(name, help_text):
        cmd = commands.add_parser(name, help=help_text)
        cmd.add_argument("--base-url", default=os.environ.get("AIDAY_BASE_URL", "http://127.0.0.1:8000"))
        return cmd

    practice = remote("practice", "Create a practice run and print connection commands")
    practice.add_argument("--tier", type=int, choices=(1, 2, 3), default=1)
    practice.add_argument("--seed", type=int, default=42)
    practice.add_argument("--count", type=int)
    practice.add_argument("--throughput", action="store_true", help="Practice the timed volume round without the 10 req/s cap")
    practice.add_argument("--json", action="store_true")
    grade = remote("grade", "Read a practice score or authenticated organizer score")
    grade.add_argument("--token", default=os.environ.get("AIDAY_TOKEN"))
    grade.add_argument("--run-id")
    grade.add_argument("--admin-token", default=os.environ.get("AIDAY_ADMIN_TOKEN"))
    grade.add_argument("--output")
    reference = remote("reference", "Validate the reference solution through HTTP (practice only)")
    reference.add_argument("--tier", type=int, choices=(1, 2, 3), default=3)
    reference.add_argument("--seed", type=int, default=42)
    reference.add_argument("--count", type=int)
    reference.add_argument("--throughput", action="store_true")
    reference.add_argument("--output")
    export = commands.add_parser("export-schemas", help="Export public policy and request schemas")
    export.add_argument("--output", default="schemas")
    leaderboard = remote("leaderboard", "Read the organizer leaderboard")
    leaderboard.add_argument("--admin-token", default=os.environ.get("AIDAY_ADMIN_TOKEN"))
    leaderboard.add_argument("--output")
    transcript = remote("transcript", "Save a replayable organizer transcript")
    transcript.add_argument("run_id")
    transcript.add_argument("--admin-token", default=os.environ.get("AIDAY_ADMIN_TOKEN"))
    transcript.add_argument("--output", required=True)
    replay = commands.add_parser("replay", help="Recompute a saved transcript's deterministic score")
    replay.add_argument("path")
    replay.add_argument("--output")
    for name, description in (("evaluate", "Evaluate a submitted Docker image with isolation and a deadline"),
                              ("benchmark", "Run three correctness-gated timed volume trials")):
        evaluate = remote(name, description)
        evaluate.add_argument("--image", required=True)
        evaluate.add_argument("--team", required=True)
        evaluate.add_argument("--tier", type=int, choices=(1, 2, 3), default=3)
        evaluate.add_argument("--admin-token", default=os.environ.get("AIDAY_ADMIN_TOKEN"))
        evaluate.add_argument("--network", default="aiday-event")
        evaluate.add_argument("--container-base-url", default="http://aiday-server:8000")
        evaluate.add_argument("--timeout", type=int, default=900, help="Overall watchdog per container, including startup")
        evaluate.add_argument("--output-dir", default="reports")
        evaluate.add_argument("--model-env", action="append", default=[],
                              choices=("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"),
                              help="Explicitly forward this model setting from the organizer environment")
        if name == "benchmark":
            evaluate.add_argument("--qualifying-run", required=True, help="Perfect correctness run ID for the same team and tier")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn
            from .api import create_app
            app = create_app(args.db, admin_token=args.admin_token, practice=not args.evaluation,
                             request_limit=args.request_limit, time_limit=args.time_limit,
                             suite_seed=args.suite_seed, suite_count=args.suite_count,
                             benchmark_count=args.benchmark_count, benchmark_seconds=args.benchmark_seconds)
            uvicorn.run(app, host=args.host, port=args.port)
        elif args.command == "practice":
            from .client import create_practice_run
            created = create_practice_run(args.base_url, tier=args.tier, seed=args.seed, count=args.count,
                                          round_type="throughput" if args.throughput else "correctness")
            if args.json:
                _json(created)
            else:
                print(f'Run {created["run_id"]}: tier {args.tier}, {created["total_episodes"]} episodes')
                print(f'export AIDAY_BASE_URL={shlex.quote(args.base_url)}')
                print(f'export AIDAY_TOKEN={shlex.quote(created["token"])}')
                print(f'uv run python -m starter.agent --tier {args.tier}')
                print('uv run aiday grade')
        elif args.command == "grade":
            if args.run_id:
                with _organizer(args) as client:
                    response = client.get(f"/admin/runs/{args.run_id}/grade")
                    response.raise_for_status()
                    _json(response.json(), args.output)
            else:
                if not args.token:
                    raise ValueError("Set AIDAY_TOKEN or pass --token")
                from .client import Client
                with Client(args.base_url, args.token) as client:
                    _json(client.report(), args.output)
        elif args.command == "reference":
            return _reference(args)
        elif args.command == "export-schemas":
            from .models import public_schema
            from .policy import POLICY
            _json(public_schema(), str(Path(args.output) / "actions.json"))
            _json(POLICY, str(Path(args.output) / "policy.json"))
        elif args.command in ("leaderboard", "transcript"):
            with _organizer(args) as client:
                route = "/admin/leaderboard" if args.command == "leaderboard" else f"/admin/runs/{args.run_id}/transcript"
                response = client.get(route)
                response.raise_for_status()
                _json(response.json(), args.output)
        elif args.command == "replay":
            from .grading import grade_run
            saved = json.loads(Path(args.path).read_text())
            run = saved.get("run", saved)
            report = grade_run(run, detailed=True)
            report["performance"] = performance_report(run, report)
            _json(report, args.output)
        elif args.command in ("evaluate", "benchmark"):
            from .runner import evaluate_image, benchmark_image
            _json(benchmark_image(args) if args.command == "benchmark" else evaluate_image(args))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
