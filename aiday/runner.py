"""Organizer-only Docker submission runner; never mounts evaluator files."""

import json
import copy
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx

from .performance import BENCHMARK_TRIALS, summarize_trials


def container_command(args, name: str, token: str) -> list[str]:
    if args.network in ("host", "none") or args.network.startswith("container:"):
        raise ValueError("Use a dedicated bridge network shared only with the event server")
    command = [
        "docker", "run", "--detach", "--pull", "never", "--name", name, "--network", args.network,
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
        "--user", "65532:65532", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "128",
        "--memory", "1g", "--cpus", "2", "--init",
        "--log-driver", "local", "--log-opt", "max-size=1m", "--log-opt", "max-file=2",
        "--env", f"AIDAY_BASE_URL={args.container_base_url}",
        "--env", f"AIDAY_TOKEN={token}", "--env", f"AIDAY_TIER={args.tier}",
    ]
    for key in getattr(args, "model_env", []):
        if key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"}:
            raise ValueError("Only explicitly supported model settings may be forwarded")
        if key not in os.environ:
            raise ValueError(f"Requested model setting {key} is absent from the environment")
        command.extend(["--env", key])
    command.extend(["--", args.image])
    return command


def _docker(command, *, check=True, timeout=30):
    return subprocess.run(command, check=check, capture_output=True, text=True, errors="replace", timeout=timeout)


def evaluate_image(args):
    """One final run per team and tier. Save private transcript plus public summary."""
    if not args.admin_token:
        raise ValueError("Set AIDAY_ADMIN_TOKEN or pass --admin-token")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    # Preflight before consuming a team's evaluation entry. Images are built in
    # advance, outside the timed evaluation; the runner does not build or pull.
    try:
        pinned_image = _docker(["docker", "image", "inspect", "--format", "{{.Id}}", args.image]).stdout.strip()
        _docker(["docker", "network", "inspect", args.network])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Docker must be running with the submission image and dedicated network already available") from exc
    name = "aiday-team-" + uuid.uuid4().hex[:16]
    pinned_args = copy.copy(args)
    pinned_args.image = pinned_image
    container_command(pinned_args, name, "preflight")
    headers = {"Authorization": "Bearer " + args.admin_token}
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)

    def private_write(path, text):
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(text)
    failure = None
    exit_code = None
    timed_out = False
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as http:
        response = http.get("/health")
        response.raise_for_status()
        if response.json().get("mode") != "evaluation":
            raise ValueError("Final submission runner requires a server started with --evaluation")
        creation = {"tier": args.tier, "team_name": args.team, "submission_image_id": pinned_image}
        if getattr(args, "round_type", "correctness") == "throughput":
            creation.update(round_type="throughput", trial=args.trial, qualifying_run_id=args.qualifying_run)
        response = http.post("/admin/runs", json=creation)
        response.raise_for_status()
        created = response.json()
        run_id = created["run_id"]
        deadline = time.monotonic() + args.timeout
        try:
            _docker(container_command(pinned_args, name, created["token"]))
            while True:
                inspected = _docker(["docker", "inspect", "--format", "{{json .State}}", name])
                state = json.loads(inspected.stdout)
                if not state.get("Running"):
                    exit_code = state.get("ExitCode")
                    break
                status = http.get(f"/admin/runs/{run_id}")
                status.raise_for_status()
                if status.json()["finished"]:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.5)
        except (OSError, ValueError, subprocess.SubprocessError, httpx.HTTPError):
            # Never include the docker command: its argv contains a run token.
            failure = "Submission infrastructure or container execution failed; review organizer logs"
        finally:
            for command in (["docker", "kill", name], ["docker", "logs", "--tail", "1000", name], ["docker", "rm", "--force", name]):
                try:
                    result = _docker(command, check=False)
                    if command[1] == "logs":
                        # Bounded log capture; output stays private and is not terminal-rendered.
                        private_write(output / f"{run_id}.log", (result.stdout + result.stderr)[-262144:])
                except (OSError, subprocess.SubprocessError):
                    failure = "Docker cleanup failed; organizer must inspect the container"
            closed = http.post(f"/admin/runs/{run_id}/close")
            closed.raise_for_status()
        transcript = http.get(f"/admin/runs/{run_id}/transcript")
        transcript.raise_for_status()
        saved = transcript.json()
        saved["execution"] = {"image": args.image, "image_id": pinned_image, "team": args.team, "exit_code": exit_code,
                              "timed_out": timed_out, "error": failure}
        path = output / f"{run_id}.transcript.json"
        private_write(path, json.dumps(saved, indent=2) + "\n")
        report = saved["report"]
        summary = {k: v for k, v in report.items() if k != "episodes"}
        summary.update(team_name=args.team, transcript=str(path), execution=saved["execution"])
        private_write(output / f"{run_id}.summary.json", json.dumps(summary, indent=2) + "\n")
        return summary


def benchmark_image(args):
    """Run the same submitted image against three separately frozen workloads."""
    runs = []
    for trial in range(1, BENCHMARK_TRIALS + 1):
        current = copy.copy(args)
        current.round_type, current.trial = "throughput", trial
        runs.append(evaluate_image(current))
    summary = {"team_name": args.team, "tier": args.tier, "qualifying_run_id": args.qualifying_run,
               **summarize_trials(runs), "runs": runs}
    path = Path(args.output_dir) / f'benchmark-{runs[0]["run_id"]}.json'
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary
