"""Optional integration check using disposable, resource-limited Docker workloads."""

from argparse import Namespace
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid

import httpx

from aiday.grading import grade_run, reference_actions
from aiday.client import Client
from aiday.runner import evaluate_image, benchmark_image


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, errors="replace", timeout=60)


def main():
    tag = uuid.uuid4().hex[:10]
    network, server = f"aiday-smoke-{tag}", f"aiday-server-smoke-{tag}"
    token = secrets.token_urlsafe(32)
    docker("network", "create", network)
    try:
        docker("run", "--detach", "--name", server, "--network", network,
               "--network-alias", "aiday-server", "-p", "127.0.0.1::8000",
               "--env", f"AIDAY_ADMIN_TOKEN={token}", "aiday-event:local",
               "aiday", "serve", "--host", "0.0.0.0", "--db", "/data/event.sqlite3", "--evaluation",
               "--benchmark-count", "12", "--benchmark-seconds", "10")
        address = docker("port", server, "8000").stdout.strip()
        base_url = "http://" + address
        deadline = time.monotonic() + 30
        while True:
            try:
                response = httpx.get(base_url + "/health", timeout=1)
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Disposable event server failed startup") from None
                time.sleep(0.2)
        with tempfile.TemporaryDirectory(prefix="aiday-docker-smoke-") as directory:
            summary = evaluate_image(Namespace(
                admin_token=token, timeout=60, network=network, container_base_url="http://aiday-server:8000",
                image="aiday-starter:local", base_url=base_url, output_dir=directory,
                team="Smoke baseline", tier=3, model_env=[],
            ))
            saved = json.loads(Path(summary["transcript"]).read_text())
            assert summary["completed_episodes"] == summary["total_episodes"] == 24, summary
            assert 0 < summary["score"] < 100, summary
            assert not summary["execution"]["timed_out"], summary
            assert summary["execution"]["error"] is None, summary
            assert grade_run(saved["run"])["score"] == summary["score"]
            assert Path(summary["transcript"]).stat().st_mode & 0o077 == 0
            # Establish a perfect fixture-driven qualifier through HTTP, pinned
            # to the real starter image. The intentionally incomplete starter
            # must subsequently fail the speed quality gate in all three trials.
            image_id = docker("image", "inspect", "--format", "{{.Id}}", "aiday-starter:local").stdout.strip()
            with httpx.Client(base_url=base_url, headers={"Authorization": "Bearer " + token}, timeout=30) as organizer:
                response = organizer.post("/admin/runs", json={"team_name": "Smoke speed", "tier": 1, "submission_image_id": image_id})
                response.raise_for_status()
                created = response.json()
                response = organizer.get(f'/admin/runs/{created["run_id"]}/transcript')
                response.raise_for_status()
                fixture = response.json()["run"]
            with Client(base_url, created["token"]) as participant:
                for episode in fixture["episodes"]:
                    mid = episode["message"]["message_id"]
                    participant.inbox()
                    for action in reference_actions(episode, 1):
                        participant._request("POST", "/v1/actions", json=action)
                    participant.complete(mid, episode["expected"]["disposition"])
            speed = benchmark_image(Namespace(
                admin_token=token, timeout=60, network=network, container_base_url="http://aiday-server:8000",
                image="aiday-starter:local", base_url=base_url, output_dir=directory,
                team="Smoke speed", tier=1, model_env=[], qualifying_run=created["run_id"],
            ))
            assert speed["trials_completed"] == 3
            assert not speed["eligible_for_tiebreak"] and speed["median_correct_per_minute"] is None
            fingerprints = set()
            for trial in speed["runs"]:
                assert trial["completed_episodes"] == 12, trial
                assert trial["performance"]["measurement_complete"]
                assert not trial["performance"]["quality_pass"]
                transcript = json.loads(Path(trial["transcript"]).read_text())
                fingerprints.add(transcript["run"]["_meta"]["suite_fingerprint"])
            assert len(fingerprints) == 3
            print(json.dumps({"docker_smoke": "passed", "episodes": 24, "baseline_score": summary["score"],
                              "replay_matches": True, "speed_trials": 3, "quality_gate_rejected_incomplete_baseline": True}))
    finally:
        docker("rm", "--force", server, check=False)
        docker("network", "rm", network, check=False)


if __name__ == "__main__":
    main()
