"""Timing, qualification, and anti-gaming tests for the optional volume round."""

import copy

from fastapi.testclient import TestClient
import pytest

from aiday.api import create_app
from aiday import engine
from aiday.grading import grade_run, reference_actions
from aiday.performance import is_perfect, performance_report, summarize_trials

ADMIN = "performance-organizer-test-token-12345"
AUTH = {"Authorization": "Bearer " + ADMIN}


def headers(created):
    return {"Authorization": "Bearer " + created["token"]}


def solve_episode(http, created, clock, duration=1):
    authorization = headers(created)
    message = http.get("/v1/inbox", headers=authorization).json()["messages"][0]
    run = http.app.state.store.read(created["run_id"])
    episode = next(ep for ep in run["episodes"] if ep["message"]["message_id"] == message["message_id"])
    if episode["expected"]["disposition"] != "ignored":
        assert http.post("/v1/query", headers=authorization, json={"message_id": message["message_id"], "sql": "SELECT * FROM orders"}).status_code == 200
    operation_id = None
    for action in reference_actions(episode, run["tier"]):
        if action["type"] == "reply.send" and operation_id:
            action["payload"]["supporting_action_ids"] = [operation_id]
        response = http.post("/v1/actions", headers=authorization, json=action)
        assert response.status_code == 200, response.text
        if action["type"].startswith("order."):
            operation_id = response.json()["action_id"]
    clock[0] += duration
    body = {"idempotency_key": "done-" + message["message_id"], "disposition": episode["expected"]["disposition"]}
    response = http.post(f'/v1/messages/{message["message_id"]}/complete', headers=authorization, json=body)
    assert response.status_code == 200, response.text
    return message, body


def create_qualifier(http, team="Team", tier=1, image_id=None):
    created = http.post("/admin/runs", headers=AUTH, json={"team_name": team, "tier": tier, "submission_image_id": image_id}).json()
    # Domain fixture solution, without timing, suffices to establish qualification.
    with http.app.state.store.transaction(created["run_id"]) as run:
        for episode in run["episodes"]:
            if episode["expected"]["disposition"] != "ignored":
                engine.query(run, {"message_id": episode["message"]["message_id"], "sql": "SELECT * FROM orders"})
            operation = None
            for action in reference_actions(episode, tier):
                if action["type"] == "reply.send" and operation:
                    action["payload"]["supporting_action_ids"] = [operation]
                receipt = engine.apply_action(run, action)
                if action["type"].startswith("order."):
                    operation = receipt["action_id"]
            engine.complete_message(run, episode["message"]["message_id"], {
                "idempotency_key": "done-" + episode["message"]["message_id"], "disposition": episode["expected"]["disposition"]})
        assert is_perfect(grade_run(run))
    return created


def benchmark(http, qualifier, team="Team", tier=1, trial=1):
    response = http.post("/admin/runs", headers=AUTH, json={"team_name": team, "tier": tier,
        "round_type": "throughput", "trial": trial, "qualifying_run_id": qualifier["run_id"]})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def clock(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("aiday.api.time.time", lambda: clock[0])
    return clock


@pytest.fixture
def evaluation():
    with TestClient(create_app(":memory:", admin_token=ADMIN, practice=False, rate_limit=False,
                              suite_count=12, benchmark_count=12, benchmark_seconds=30)) as http:
        yield http


def test_fully_solved_round_has_server_timing_latency_and_retry_stability(evaluation, clock):
    qualifier = create_qualifier(evaluation, tier=3)
    created = benchmark(evaluation, qualifier, tier=3)
    clock[0] += 50  # Container preparation is outside the measurement.
    assert evaluation.get(f'/admin/runs/{created["run_id"]}/grade', headers=AUTH).json()["performance"]["elapsed_seconds"] is None
    for _ in range(12):
        message, body = solve_episode(evaluation, created, clock, duration=2)
    report = evaluation.get(f'/admin/runs/{created["run_id"]}/grade', headers=AUTH).json()
    perf = report["performance"]
    assert report["score"] == 100
    assert perf["eligible_for_tiebreak"] and perf["quality_pass"]
    assert perf["elapsed_seconds"] == 24
    assert perf["correct_per_minute"] == 30
    assert perf["latency_p50_seconds"] == perf["latency_p95_seconds"] == 2
    clock[0] += 100
    assert evaluation.post(f'/v1/messages/{message["message_id"]}/complete', headers=headers(created), json=body).status_code == 200
    after = evaluation.get(f'/admin/runs/{created["run_id"]}/grade', headers=AUTH).json()["performance"]
    assert after["elapsed_seconds"] == 24 and after["correct_requests"] == 12


def test_early_exit_cannot_game_rate_and_deadline_uses_entire_window(evaluation, clock):
    qualifier = create_qualifier(evaluation)
    early = benchmark(evaluation, qualifier, trial=1)
    solve_episode(evaluation, early, clock)
    evaluation.post(f'/admin/runs/{early["run_id"]}/close', headers=AUTH)
    perf = evaluation.get(f'/admin/runs/{early["run_id"]}/grade', headers=AUTH).json()["performance"]
    assert perf["quality_pass"] and not perf["measurement_complete"] and not perf["eligible_for_tiebreak"]
    timed = benchmark(evaluation, qualifier, trial=2)
    started = clock[0]
    solve_episode(evaluation, timed, clock)
    clock[0] = started + 35
    state = evaluation.get("/v1/run", headers=headers(timed)).json()
    assert state["finished"] and state["close_reason"] == "deadline"
    perf = evaluation.get(f'/admin/runs/{timed["run_id"]}/grade', headers=AUTH).json()["performance"]
    assert perf["eligible_for_tiebreak"] and perf["elapsed_seconds"] == 30
    assert perf["correct_per_minute"] == 2


def test_failed_or_foreign_qualifier_and_duplicate_trials_are_rejected(evaluation):
    failed = evaluation.post("/admin/runs", headers=AUTH, json={"team_name": "Failed"}).json()
    evaluation.post(f'/admin/runs/{failed["run_id"]}/close', headers=AUTH)
    for qualifier, team in ((failed, "Failed"), (create_qualifier(evaluation), "Another team")):
        response = evaluation.post("/admin/runs", headers=AUTH, json={"team_name": team, "round_type": "throughput", "tier": 1,
            "qualifying_run_id": qualifier["run_id"]})
        assert response.status_code == 422 and response.json()["error"]["code"] == "qualification_required"
    qualifier = create_qualifier(evaluation, team="Unique")
    benchmark(evaluation, qualifier, team="Unique")
    response = evaluation.post("/admin/runs", headers=AUTH, json={"team_name": "unique", "round_type": "throughput", "tier": 1,
        "qualifying_run_id": qualifier["run_id"]})
    assert response.status_code == 409


def test_workloads_frozen_per_trial_and_shared_between_teams(evaluation):
    first = create_qualifier(evaluation, team="First")
    second = create_qualifier(evaluation, team="Second")
    entries = [benchmark(evaluation, first, team="First", trial=1), benchmark(evaluation, second, team="Second", trial=1),
               benchmark(evaluation, first, team="First", trial=2)]
    runs = [evaluation.app.state.store.read(entry["run_id"]) for entry in entries]
    assert runs[0]["episodes"] == runs[1]["episodes"]
    assert runs[0]["_meta"]["suite_fingerprint"] != runs[2]["_meta"]["suite_fingerprint"]


def test_speed_round_requires_the_qualifying_immutable_image(evaluation):
    image_id = "sha256:" + "a" * 64
    qualifier = create_qualifier(evaluation, image_id=image_id)
    body = {"team_name": "Team", "tier": 1, "round_type": "throughput", "qualifying_run_id": qualifier["run_id"]}
    for wrong_image in (None, "sha256:" + "b" * 64):
        response = evaluation.post("/admin/runs", headers=AUTH, json=body | {"submission_image_id": wrong_image})
        assert response.status_code == 422 and response.json()["error"]["code"] == "submission_mismatch"
    response = evaluation.post("/admin/runs", headers=AUTH, json=body | {"submission_image_id": image_id})
    assert response.status_code == 201


def test_throughput_relaxes_cap_and_practice_cannot_qualify_for_prizes(clock):
    with TestClient(create_app(":memory:", benchmark_count=12, benchmark_seconds=10)) as http:
        normal = http.post("/practice/runs", json={}).json()
        burst = http.post("/practice/runs", json={"round_type": "throughput", "count": 12}).json()
        assert any(http.get("/v1/inbox", headers=headers(normal)).status_code == 429 for _ in range(25))
        assert all(http.get("/v1/inbox", headers=headers(burst)).status_code == 200 for _ in range(25))
        assert http.get("/v1/run", headers=headers(burst)).json()["limits"]["requests_per_second"] is None
        clock[0] += 10
        perf = http.get("/v1/report", headers=headers(burst)).json()["performance"]
        assert not perf["eligible_for_tiebreak"]


def test_incorrect_completed_cases_or_unfinished_effects_fail_quality(evaluation, clock):
    qualifier = create_qualifier(evaluation, tier=3)
    created = benchmark(evaluation, qualifier, tier=3)
    solve_episode(evaluation, created, clock)
    clock[0] += 40
    evaluation.get("/v1/run", headers=headers(created))
    run = evaluation.app.state.store.read(created["run_id"])
    assert performance_report(run)["eligible_for_tiebreak"]
    dirty = copy.deepcopy(run)
    dirty["episodes"][1]["events"].append({"type": "action.accepted", "payload": {"type": "reply.send"}})
    assert not performance_report(dirty)["eligible_for_tiebreak"]
    assert performance_report(dirty)["unfinished_business_effects"]
    wrong = copy.deepcopy(run)
    wrong["episodes"][0]["disposition"] = "wrong"
    assert not performance_report(wrong)["quality_pass"]


def test_median_requires_every_trial_and_ignores_single_fast_outlier():
    def report(trial, rate):
        return {"performance": {"trial": trial, "correct_per_minute": rate,
            "eligible_for_tiebreak": True, "qualifying_run_id": "qualified"}}
    reports = [report(1, 60), report(2, 60000), report(3, 62)]
    assert summarize_trials(reports)["median_correct_per_minute"] == 62
    assert not summarize_trials(reports[:2])["eligible_for_tiebreak"]
    assert not summarize_trials([reports[0], reports[0], reports[2]])["eligible_for_tiebreak"]
    assert not is_perfect({"score": 100, "total_episodes": 12, "completed_episodes": 12, "fully_correct_episodes": 11})


def test_leaderboard_breaks_perfect_ties_by_median_valid_throughput(evaluation, clock):
    for team, seconds in (("Fast", 1), ("Slower", 2)):
        qualifier = create_qualifier(evaluation, team=team)
        for trial in (1, 2, 3):
            created = benchmark(evaluation, qualifier, team=team, trial=trial)
            for _ in range(12):
                solve_episode(evaluation, created, clock, duration=seconds)
    rows = evaluation.get("/admin/leaderboard", headers=AUTH).json()["entries"]
    assert len(rows) == 2
    assert [(row["team_name"], row["rank"]) for row in rows] == [("Fast", 1), ("Slower", 2)]
    assert rows[0]["throughput"]["median_correct_per_minute"] == 60
    assert rows[1]["throughput"]["median_correct_per_minute"] == 30


def test_benchmark_profile_persists_and_rejects_configuration_changes(tmp_path):
    path = tmp_path / "frozen.sqlite3"
    kwargs = dict(admin_token=ADMIN, practice=False, suite_count=12, benchmark_count=12, benchmark_seconds=30)
    first = create_app(path, **kwargs)
    frozen = first.state.store.setting("benchmark_suite", {})
    first.state.store.close()
    second = create_app(path, **kwargs)
    assert second.state.store.setting("benchmark_suite", {}) == frozen
    second.state.store.close()
    with pytest.raises(ValueError, match="Benchmark profile is already frozen"):
        create_app(path, **(kwargs | {"benchmark_seconds": 31}))
