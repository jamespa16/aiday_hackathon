"""Server-owned timing and correctness-gated throughput measurements.

These metrics are separate from deterministic correctness points. All durations
come from the event server, never a participant-submitted timestamp.
"""

import math
from statistics import median

from .grading import grade_run

BENCHMARK_TRIALS = 3
BENCHMARK_COUNT = 240
BENCHMARK_SECONDS = 120


def is_perfect(report):
    return (report["total_episodes"] > 0 and
            report["fully_correct_episodes"] == report["completed_episodes"] == report["total_episodes"])


def begin_work(run, now):
    meta = run["_meta"]
    timing = meta.setdefault("timing", {"started_at": None, "finished_at": None, "handler_seconds": 0.0})
    if timing["started_at"] is None and not run["finished"]:
        timing["started_at"] = now
        if meta.get("round_type") == "throughput":
            meta["deadline"] = now + meta["benchmark"]["duration_seconds"]
        run["episodes"][run["episode_index"]].setdefault("_timing", {})["activated_at"] = now


def record_progress(run, previous_index, now):
    timing = run["_meta"].get("timing", {})
    if timing.get("started_at") is None:
        return
    if run["episode_index"] > previous_index:
        for episode in run["episodes"][previous_index:run["episode_index"]]:
            episode.setdefault("_timing", {})["completed_at"] = now
        if not run["finished"]:
            run["episodes"][run["episode_index"]].setdefault("_timing", {})["activated_at"] = now
    if run["finished"] and timing.get("finished_at") is None:
        timing["finished_at"] = min(now, run["_meta"]["deadline"])


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lo, hi = math.floor(position), math.ceil(position)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo), 6)


def performance_report(run, report=None):
    report = report if report is not None else grade_run(run, detailed=False)
    meta = run.get("_meta", {})
    timing = meta.get("timing", {})
    benchmark = meta.get("benchmark", {})
    started, finished = timing.get("started_at"), timing.get("finished_at")
    elapsed = finished - started if started is not None and finished is not None else None
    measured = elapsed is not None and elapsed > 0 and math.isfinite(elapsed)
    full = report["completed_episodes"] == report["total_episodes"]
    window = benchmark.get("duration_seconds")
    # A partial workload must consume the full window. Early exit cannot turn
    # a short, easy prefix into an artificially high throughput result.
    entire_window = bool(measured and window and elapsed >= window - 1e-6 and meta.get("close_reason") == "deadline")
    valid = bool(run["finished"] and measured and (full or entire_window))
    completed, correct = report["completed_episodes"], report["fully_correct_episodes"]
    unfinished_effects = any(not episode.get("completed") and any(
        event.get("type") == "action.accepted" and
        (event.get("payload", {}).get("type", "").startswith("order.") or
         event.get("payload", {}).get("type") == "reply.send")
        for event in episode.get("events", [])) for episode in run["episodes"])
    quality = bool(completed and correct == completed and report["prohibited_attempts"] == 0 and not unfinished_effects)
    eligible = bool(meta.get("round_type") == "throughput" and benchmark.get("qualified") and valid and quality)
    durations = []
    for episode in run["episodes"]:
        marks = episode.get("_timing", {})
        if episode.get("completed") and "activated_at" in marks and "completed_at" in marks:
            duration = marks["completed_at"] - marks["activated_at"]
            if duration >= 0:
                durations.append(duration)
    reasons = []
    if not benchmark.get("qualified"):
        reasons.append("no_perfect_qualifying_run")
    if not valid:
        reasons.append("incomplete_timing_or_early_exit")
    if not quality:
        reasons.append("incorrect_completed_requests_prohibited_attempts_or_unfinished_effects")
    return {
        "round_type": meta.get("round_type", "correctness"),
        "trial": benchmark.get("trial"), "qualifying_run_id": benchmark.get("qualifying_run_id"),
        "elapsed_seconds": round(elapsed, 6) if measured else None,
        "measurement_complete": valid,
        "completed_requests": completed, "correct_requests": correct,
        "accuracy": correct / completed if completed else 0.0,
        "completed_per_minute": round(completed * 60 / elapsed, 6) if measured else None,
        "correct_per_minute": round(correct * 60 / elapsed, 6) if measured else None,
        "latency_p50_seconds": _percentile(durations, .5),
        "latency_p95_seconds": _percentile(durations, .95),
        "api_requests": meta.get("requests", 0),
        "server_handler_seconds": round(timing.get("handler_seconds", 0.0), 6),
        "quality_pass": quality, "eligible_for_tiebreak": eligible,
        "unfinished_business_effects": unfinished_effects,
        "ineligibility_reasons": [] if eligible else reasons,
        "workload_exhausted": full,
    }


def summarize_trials(reports):
    performances = [report["performance"] for report in reports]
    by_trial = {p["trial"]: p for p in performances}
    references = {p["qualifying_run_id"] for p in performances}
    eligible = (len(performances) == BENCHMARK_TRIALS and set(by_trial) == {1, 2, 3} and
                len(references) == 1 and all(p["eligible_for_tiebreak"] for p in performances))
    return {
        "trials_completed": len(performances), "trials_required": BENCHMARK_TRIALS,
        "eligible_for_tiebreak": bool(eligible),
        "median_correct_per_minute": round(median(p["correct_per_minute"] for p in performances), 3) if eligible else None,
        "trials": sorted(performances, key=lambda p: p["trial"] or 0),
    }
