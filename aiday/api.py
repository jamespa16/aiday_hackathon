"""Authenticated HTTP boundary for practice and organizer evaluation."""

from collections import defaultdict
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from . import engine
from .grading import grade_run
from .policy import POLICY
from .scenarios import generate_suite
from .storage import Store
from .performance import (BENCHMARK_COUNT, BENCHMARK_SECONDS, BENCHMARK_TRIALS,
                          begin_work, is_perfect, performance_report, record_progress, summarize_trials)


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seed: int | None = None
    count: int | None = Field(default=None, ge=1, le=1200)
    tier: int = Field(default=3, ge=1, le=3)
    team_name: str = Field(default="practice", min_length=1, max_length=100)
    round_type: Literal["correctness", "throughput"] = "correctness"
    trial: int = Field(default=1, ge=1, le=BENCHMARK_TRIALS)
    qualifying_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    submission_image_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


def _error(status: int, code: str, message: str, retryable: bool = False):
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "retryable": retryable}})


class BodyLimit:
    def __init__(self, app, maximum=65536):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.maximum:
                return await _error(413, "body_too_large", "Request exceeds 64 KiB")(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(db_path: str = ".aiday/event.sqlite3", admin_token: str | None = None,
               practice: bool = True, request_limit: int = 2000, time_limit: int = 900,
               rate_limit: bool = True, suite_seed: int | None = None, suite_count: int = 24,
               benchmark_count: int = BENCHMARK_COUNT, benchmark_seconds: int = BENCHMARK_SECONDS) -> FastAPI:
    if type(benchmark_count) is not int or not 12 <= benchmark_count <= 1200 or benchmark_count % 12:
        raise ValueError("benchmark_count must be a multiple of 12 from 12 through 1200")
    if type(benchmark_seconds) is not int or not 1 <= benchmark_seconds <= 900:
        raise ValueError("benchmark_seconds must be 1..900")
    admin_secret = admin_token or os.environ.get("AIDAY_ADMIN_TOKEN")
    if not practice and (not admin_secret or len(admin_secret) < 24):
        raise ValueError("Evaluation mode requires AIDAY_ADMIN_TOKEN with at least 24 characters")
    store = Store(str(db_path))
    evaluation_suite = None
    benchmark_suite = None
    if not practice:
        if not 1 <= suite_count <= 200:
            raise ValueError("suite_count must be 1..200")
        seed = suite_seed if suite_seed is not None else secrets.randbits(128)
        rules_hash = hashlib.sha256(b"".join(Path(__file__).with_name(name + ".py").read_bytes() for name in (
            "policy", "models", "engine", "sql", "scenarios", "grading", "performance", "api", "storage", "runner"))).hexdigest()
        evaluation_suite = store.setting("evaluation_suite", {
            "seed": seed, "count": suite_count, "episodes": generate_suite(seed=seed, count=suite_count),
            "policy": POLICY, "rules_fingerprint": rules_hash,
        })
        if suite_seed is not None and suite_seed != evaluation_suite["seed"] or suite_count != evaluation_suite["count"]:
            store.close()
            raise ValueError("Evaluation suite is already frozen; use its existing configuration or a new database")
        if evaluation_suite.get("rules_fingerprint") != rules_hash or evaluation_suite.get("policy") != POLICY:
            store.close()
            raise ValueError("Evaluation rules changed since this database was frozen; restore the frozen release or use a new database")
        benchmark_suite = store.setting("benchmark_suite", {
            "count": benchmark_count, "duration_seconds": benchmark_seconds,
            "trials": [generate_suite(seed=secrets.randbits(128), count=benchmark_count) for _ in range(BENCHMARK_TRIALS)],
        })
        if benchmark_suite["count"] != benchmark_count or benchmark_suite["duration_seconds"] != benchmark_seconds:
            store.close()
            raise ValueError("Benchmark profile is already frozen; use its existing configuration or a new database")
    buckets = defaultdict(lambda: [20.0, time.monotonic()])

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.close()

    app = FastAPI(title="AI Day Support Challenge", version=__version__, lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.store = store
    app.state.practice = practice

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if isinstance(exc.detail, dict):
            return _error(exc.status_code, exc.detail.get("code", "http_error"), exc.detail.get("message", "Request rejected"), exc.status_code == 429)
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo request bodies or credentials into errors.
        return _error(422, "schema_violation", "Request does not match the published schema")

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer ") or not authorization[7:]:
            raise HTTPException(401, {"code": "unauthorized", "message": "A Bearer token is required"})
        return authorization[7:]

    def participant(authorization: Annotated[str | None, Header()] = None) -> str:
        token = bearer(authorization)
        run_id = store.run_id_for_token(token)
        if run_id is None:
            raise HTTPException(401, {"code": "unauthorized", "message": "Unknown run token"})
        return run_id

    def admin(authorization: Annotated[str | None, Header()] = None):
        token = bearer(authorization)
        if not admin_secret or not hmac.compare_digest(token.encode(), admin_secret.encode()):
            raise HTTPException(401, {"code": "unauthorized", "message": "Organizer credentials required"})

    def run_exists(run_id):
        try:
            return store.read(run_id)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Run not found"}) from None

    def metadata(run):
        meta = run["_meta"]
        ep = None if run["finished"] else engine.active_episode(run)
        return {
            "schema_version": "1.0", "run_id": run["run_id"], "tier": run["tier"],
            "team_name": meta["team_name"], "finished": run["finished"],
            "episode_id": ep["episode_id"] if ep else None,
            "round_type": meta.get("round_type", "correctness"),
            "benchmark": {k: meta["benchmark"][k] for k in ("trial", "duration_seconds")} if meta.get("round_type") == "throughput" else None,
            "simulation_time": ep["now"] if ep else None,
            "completed_episodes": sum(bool(e["completed"]) for e in run["episodes"]),
            "total_episodes": len(run["episodes"]), "close_reason": meta.get("close_reason"),
            "limits": {"request_limit": meta["request_limit"], "time_limit_seconds": meta["time_limit"],
                       "remaining_requests": max(0, meta["request_limit"] - meta["requests"]),
                       "remaining_seconds": max(0, round(meta["deadline"] - time.time(), 2)),
                       "requests_per_second": None if meta.get("round_type") == "throughput" else 10,
                       "burst": None if meta.get("round_type") == "throughput" else 20,
                       "body_bytes": 65536, "sql_rows": 200},
        }

    def expire(run):
        if not run["finished"] and time.time() >= run["_meta"]["deadline"]:
            run["finished"] = True
            run["_meta"]["close_reason"] = "deadline"
            record_progress(run, run["episode_index"], run["_meta"]["deadline"])

    def invoke(run_id, operation, *, count=True, throttle=True, participant_request=False):
        result = failure = None
        handler_started = time.perf_counter()
        with store.transaction(run_id) as run:
            expire(run)
            meta = run["_meta"]
            previous_index = run["episode_index"]
            admitted_at = time.time()
            if count or participant_request:
                begin_work(run, admitted_at)
            if count and meta["requests"] >= meta["request_limit"]:
                run["finished"] = True
                meta["close_reason"] = "request_limit"
                failure = _error(429, "request_limit", "Run request budget exhausted; retrieve /v1/report or ask the organizer for results")
            elif rate_limit and throttle and meta.get("round_type") != "throughput":
                now = time.monotonic()
                bucket = buckets[run_id]
                bucket[0] = min(20.0, bucket[0] + (now - bucket[1]) * 10)
                bucket[1] = now
                if bucket[0] < 1:
                    failure = _error(429, "rate_limit", "Retry after a short delay", True)
                    failure.headers["Retry-After"] = "1"
                else:
                    bucket[0] -= 1
            if failure is None:
                if count:
                    meta["requests"] += 1
                try:
                    result = operation(run)
                except engine.EngineError as exc:
                    failure = _error(exc.status_code, exc.code, exc.message)
            record_progress(run, previous_index, admitted_at)
            if count or participant_request:
                timing = meta.get("timing", {})
                timing["handler_seconds"] = timing.get("handler_seconds", 0.0) + time.perf_counter() - handler_started
        return failure if failure is not None else result

    def scored(run, detailed=True):
        report = grade_run(run, detailed=detailed)
        report["performance"] = performance_report(run, report)
        return report

    def create_locked(body: CreateRun):
        qualified = False
        if body.round_type == "correctness" and (body.trial != 1 or body.qualifying_run_id is not None):
            raise HTTPException(422, {"code": "invalid_round", "message": "Trial and qualification options are for throughput rounds only"})
        if body.round_type == "throughput" and not practice:
            qualifier = run_exists(body.qualifying_run_id) if body.qualifying_run_id else None
            qualified = bool(qualifier and qualifier["finished"] and qualifier["tier"] == body.tier and
                             qualifier["_meta"].get("mode") == "evaluation" and
                             qualifier["_meta"].get("round_type", "correctness") == "correctness" and
                             qualifier["_meta"]["team_name"].strip().casefold() == body.team_name.strip().casefold() and
                             is_perfect(grade_run(qualifier, detailed=False)))
            if not qualified:
                raise HTTPException(422, {"code": "qualification_required", "message": "Throughput requires a finished, perfect correctness run for this team and tier"})
            if qualifier["_meta"].get("submission_image_id") != body.submission_image_id:
                raise HTTPException(422, {"code": "submission_mismatch", "message": "Use the same immutable submission image as the qualifying correctness run"})
        if evaluation_suite is not None:
            selected_count = benchmark_suite["count"] if body.round_type == "throughput" else evaluation_suite["count"]
            if body.seed is not None and (body.round_type == "throughput" or body.seed != evaluation_suite["seed"]) or body.count is not None and body.count != selected_count:
                raise HTTPException(409, {"code": "suite_frozen", "message": "Evaluation runs must use the frozen suite; omit seed and count"})
            chosen_seed, chosen_count = evaluation_suite["seed"], selected_count
            if body.round_type == "correctness" and any(r.get("_meta", {}).get("mode") == "evaluation" and
                   r.get("_meta", {}).get("round_type", "correctness") == "correctness" and r["tier"] == body.tier and
                   r["_meta"]["team_name"].strip().casefold() == body.team_name.strip().casefold() for r in store.all()):
                raise HTTPException(409, {"code": "duplicate_entry", "message": "This team already has an evaluation run at this tier"})
        else:
            chosen_seed = body.seed if body.seed is not None else 42
            chosen_count = body.count if body.count is not None else (benchmark_count if body.round_type == "throughput" else 12)
        fixtures = (benchmark_suite["trials"][body.trial - 1] if benchmark_suite is not None and body.round_type == "throughput" else
                    evaluation_suite["episodes"] if evaluation_suite is not None else generate_suite(seed=chosen_seed, count=chosen_count))
        run = engine.new_run(fixtures, tier=body.tier)
        created = time.time()
        run["_meta"] = {"team_name": body.team_name, "seed": chosen_seed, "mode": "practice" if practice else "evaluation", "created_at": created,
                        "deadline": created + time_limit, "time_limit": benchmark_seconds if body.round_type == "throughput" else time_limit,
                        "request_limit": max(request_limit, chosen_count * 30) if body.round_type == "throughput" else request_limit, "requests": 0,
                        "round_type": body.round_type,
                        "submission_image_id": body.submission_image_id,
                        "policy_version": POLICY["policy_version"], "generator_version": "1.0",
                        "suite_fingerprint": hashlib.sha256(json.dumps(run["episodes"], sort_keys=True).encode()).hexdigest()}
        if body.round_type == "throughput":
            run["_meta"]["benchmark"] = {"trial": body.trial, "duration_seconds": benchmark_seconds,
                                         "qualified": qualified, "qualifying_run_id": body.qualifying_run_id}
        try:
            token = store.create(run)
        except sqlite3.IntegrityError:
            raise HTTPException(409, {"code": "duplicate_entry", "message": "This team already has an entry for this tier, round, and trial"}) from None
        return {"run_id": run["run_id"], "token": token, "tier": run["tier"], "total_episodes": len(run["episodes"])}

    def create(body: CreateRun):
        with store.lock:
            return create_locked(body)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "mode": "practice" if practice else "evaluation"}

    @app.get("/v1/policy")
    def policy():
        return POLICY

    @app.get("/v1/schema")
    def schema():
        from .models import public_schema
        return public_schema()

    @app.get("/v1/run")
    def get_run(run_id: str = Depends(participant)):
        return invoke(run_id, metadata, count=False, throttle=False, participant_request=True)

    @app.get("/v1/inbox")
    def inbox(cursor: str | None = None, limit: int = 20, run_id: str = Depends(participant)):
        if limit < 1 or limit > 100:
            raise HTTPException(422, {"code": "schema_violation", "message": "limit must be 1..100"})
        def get(run):
            # Opaque cursors identify a position without granting authority to advance it.
            current = hashlib.sha256(f'{run_id}:{run["episode_index"]}'.encode()).hexdigest()[:24]
            valid = cursor is None or any(hmac.compare_digest(cursor.encode(), hashlib.sha256(f"{run_id}:{i}".encode()).hexdigest()[:24].encode()) for i in range(len(run["episodes"]) + 1))
            if not valid:
                raise engine.EngineError(400, "invalid_cursor", "Cursor does not belong to this run")
            ep = None if run["finished"] else engine.active_episode(run)
            message = ep["message"] if ep else None
            return {"messages": [message] if message else [], "next_cursor": current, "finished": run["finished"]}
        return invoke(run_id, get)

    @app.get("/v1/issues")
    def issues(run_id: str = Depends(participant)):
        def get(run):
            ep = None if run["finished"] else engine.active_episode(run)
            return {"issues": list(ep["issues"].values()) if ep else []}
        return invoke(run_id, get)

    @app.post("/v1/query")
    def query(body: dict, run_id: str = Depends(participant)):
        return invoke(run_id, lambda run: engine.query(run, body))

    @app.post("/v1/actions")
    def action(body: dict, run_id: str = Depends(participant)):
        return invoke(run_id, lambda run: engine.apply_action(run, body))

    @app.post("/v1/messages/{message_id}/complete")
    def complete(message_id: str, body: dict, run_id: str = Depends(participant)):
        return invoke(run_id, lambda run: engine.complete_message(run, message_id, body))

    @app.get("/v1/actions/{action_id}")
    def receipt(action_id: str, run_id: str = Depends(participant)):
        def get(run):
            for saved in run["receipts"].values():
                item = saved.get("receipt", {})
                if item.get("action_id") == action_id:
                    return item
            raise engine.EngineError(404, "not_found", "Action not found")
        return invoke(run_id, get, count=False, throttle=False)

    if practice:
        @app.post("/practice/runs", status_code=201)
        def practice_run(body: CreateRun):
            return create(body)

        @app.get("/v1/report")
        def report(run_id: str = Depends(participant)):
            return invoke(run_id, lambda run: scored(run), count=False, throttle=False)

    @app.post("/admin/runs", status_code=201, dependencies=[Depends(admin)])
    def create_admin_run(body: CreateRun):
        return create(body)

    @app.get("/admin/runs", dependencies=[Depends(admin)])
    def list_runs():
        return {"runs": [metadata(run) for run in store.all()]}

    @app.get("/admin/runs/{run_id}", dependencies=[Depends(admin)])
    def get_admin_run(run_id: str):
        run_exists(run_id)
        return invoke(run_id, metadata, count=False, throttle=False)

    @app.post("/admin/runs/{run_id}/close", dependencies=[Depends(admin)])
    def close_run(run_id: str):
        run_exists(run_id)
        def close(run):
            if not run["finished"]:
                run["finished"] = True
                run["_meta"]["close_reason"] = "organizer"
                record_progress(run, run["episode_index"], time.time())
            return metadata(run)
        return invoke(run_id, close, count=False, throttle=False)

    @app.get("/admin/runs/{run_id}/grade", dependencies=[Depends(admin)])
    def admin_grade(run_id: str):
        run_exists(run_id)
        return invoke(run_id, lambda run: scored(run), count=False, throttle=False)

    @app.get("/admin/runs/{run_id}/transcript", dependencies=[Depends(admin)])
    def transcript(run_id: str):
        run_exists(run_id)
        return invoke(run_id, lambda run: {"schema_version": "1.0", "run": run, "report": scored(run)}, count=False, throttle=False)

    @app.get("/admin/leaderboard", dependencies=[Depends(admin)])
    def leaderboard():
        entries = []
        throughput = defaultdict(list)
        for run in store.all():
            if run["_meta"].get("round_type") == "throughput":
                if run["finished"]:
                    throughput[run["_meta"]["benchmark"]["qualifying_run_id"]].append(scored(run, detailed=False))
                continue
            if not run["finished"]:
                continue
            report = grade_run(run, detailed=False)
            writes = sum(e.get("type") == "action.accepted" for e in run["events"])
            entries.append({"run_id": run["run_id"], "team_name": run["_meta"]["team_name"], "accepted_write_actions": writes, **report})
        for entry in entries:
            entry["throughput"] = summarize_trials(throughput.get(entry["run_id"], []))
        def order(row):
            speed = row["throughput"]["median_correct_per_minute"] if is_perfect(row) and row["throughput"]["eligible_for_tiebreak"] else 0
            return (row["tier"], -row["score"], -row["fully_correct_episodes"], -speed, row["prohibited_attempts"], row["accepted_write_actions"])
        entries.sort(key=order)
        previous, rank, position, previous_tier = None, 0, 0, None
        for entry in entries:
            position = position + 1 if previous_tier == entry["tier"] else 1
            if order(entry) != previous:
                rank = position
            entry["rank"] = rank
            previous, previous_tier = order(entry), entry["tier"]
        return {"entries": entries}

    return app
