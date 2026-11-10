"""Evaluation engine: runs the JSON cases against the real harness.

Each case gets an isolated SQLite database, the same tools/approval/verification
path as production, and an optional fault injection. Deterministic metrics are
computed in code, never judged by a model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from config import HarnessConfig, ProviderSettings, RESULTS_DIR, Settings
from storage.models import EvalRecord, new_id
from storage.repositories import Repositories
from storage.services import ensure_user, ingest_dataset_file

from agent.harness import AgentHarness
from agent.provider import DeterministicPlanner
from evaluation.metrics import render_markdown, summarize

EVAL_DIR = Path(__file__).resolve().parent
CASES_PATH = EVAL_DIR / "test_cases.json"
FIXTURES_DIR = EVAL_DIR.parent / "data" / "fixtures"
WORK_DIR = RESULTS_DIR / "_work"


@dataclass
class EvalCase:
    id: str
    category: str
    description: str
    user_request: str
    dataset: str
    action: str
    expect: dict[str, Any]
    profile: dict[str, Any]
    faults: dict[str, int]
    repeat: int


def load_cases(limit: int | None = None) -> tuple[list[EvalCase], int]:
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = [
        EvalCase(
            id=item["id"],
            category=item["category"],
            description=item["description"],
            user_request=item["user_request"],
            dataset=item["dataset"],
            action=item.get("action", "none"),
            expect=item.get("expect", {}),
            profile=item.get("profile", {}),
            faults=item.get("faults", {}),
            repeat=int(item.get("repeat", 1)),
        )
        for item in raw["cases"]
    ]
    if limit:
        cases = cases[:limit]
    return cases, int(raw.get("min_days", 14))


def _cleanup_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _independent_trend(dataset_path: Path, metric: str) -> tuple[float, float]:
    frame = pd.read_csv(dataset_path, parse_dates=["date"])
    end = frame["date"].max()
    recent_start = end - pd.Timedelta(days=6)
    comparison_end = recent_start - pd.Timedelta(days=1)
    comparison_start = comparison_end - pd.Timedelta(days=6)
    recent = float(
        frame[(frame["date"] >= recent_start) & (frame["date"] <= end)][metric].mean()
    )
    comparison = float(
        frame[
            (frame["date"] >= comparison_start) & (frame["date"] <= comparison_end)
        ][metric].mean()
    )
    return recent, comparison


def _case_setup(case: EvalCase, db_path: Path, min_days: int):
    repos = Repositories.open(str(db_path))
    profile = {
        "goals": ["improve_sleep", "increase_daily_activity"],
        "preferences": {
            "exercise_intensity": "low",
            "preferred_activity": ["walking"],
            "available_time_per_day": 30,
        },
        "sleep_goal_hours": 7.5,
        "daily_steps_goal": 8000,
    }
    overrides = case.profile or {}
    if "goals" in overrides:
        profile["goals"] = overrides["goals"]
    if "available_time_per_day" in overrides:
        profile["preferences"]["available_time_per_day"] = int(
            overrides["available_time_per_day"]
        )
    if "exercise_intensity" in overrides:
        profile["preferences"]["exercise_intensity"] = overrides["exercise_intensity"]

    user_id = f"eval_{case.id}"
    ensure_user(
        repos,
        user_id,
        display_name=case.id,
        goals=profile["goals"],
        preferences=profile["preferences"],
        sleep_goal_hours=profile["sleep_goal_hours"],
        daily_steps_goal=profile["daily_steps_goal"],
    )
    dataset, report = ingest_dataset_file(
        repos,
        user_id=user_id,
        path=FIXTURES_DIR / f"{case.dataset}.csv",
        min_days=min_days,
    )
    settings = Settings(
        harness=HarnessConfig(db_path=str(db_path), min_analysis_days=min_days),
        provider=ProviderSettings(provider="mock"),
    )
    harness = AgentHarness(
        settings=settings,
        repos=repos,
        provider=DeterministicPlanner(),
        fault_injection=dict(case.faults),
    )
    return repos, harness, user_id, dataset, report


def run_case(case: EvalCase, min_days: int) -> dict[str, Any]:
    start = time.perf_counter()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    db_path = WORK_DIR / f"{case.id}.db"
    _cleanup_db(db_path)

    failures: list[str] = []
    checks: dict[str, bool | None] = {}
    result: dict[str, Any] = {
        "id": case.id,
        "category": case.category,
        "description": case.description,
        "user_request": case.user_request,
    }

    try:
        repos, harness, user_id, dataset, report = _case_setup(case, db_path, min_days)
        run_id = harness.start_run(case.user_request, user_id, dataset.dataset_id)
        outcome = harness.run(run_id)
        approval_requested = outcome.pending_approval is not None
        approval_granted = False

        if case.action == "approve":
            if outcome.status == "WAITING_FOR_APPROVAL" and outcome.pending_approval:
                outcome = harness.approve(
                    run_id,
                    outcome.pending_approval["approval_id"],
                    approved=True,
                    reason="eval auto-approve",
                )
                approval_granted = True
            else:
                failures.append("expected the harness to pause for approval, but it did not")
        elif case.action == "reject":
            if outcome.status == "WAITING_FOR_APPROVAL" and outcome.pending_approval:
                outcome = harness.approve(
                    run_id,
                    outcome.pending_approval["approval_id"],
                    approved=False,
                    reason="eval auto-reject",
                )
            else:
                failures.append("expected the harness to pause for approval, but it did not")

        first_outcome = outcome
        duplicate_flag = False
        if case.repeat > 1:
            for _ in range(case.repeat - 1):
                if outcome.status != "COMPLETED":
                    failures.append("first run did not complete; duplicate replay skipped")
                    break
                second_run = harness.start_run(case.user_request, user_id, dataset.dataset_id)
                second = harness.run(second_run)
                if second.status == "WAITING_FOR_APPROVAL" and second.pending_approval:
                    second = harness.approve(
                        second_run,
                        second.pending_approval["approval_id"],
                        approved=True,
                        reason="eval duplicate replay",
                    )
                outcome = second
                write_result = (second.state.write_result if second.state else None) or {}
                duplicate_flag = duplicate_flag or bool(write_result.get("duplicate"))

        tasks = repos.tasks.list_all(user_id)
        used_tools = list(dict.fromkeys(first_outcome.state.used_tools if first_outcome.state else []))
        final_answer = outcome.final_answer or ""
        dataset_state = (first_outcome.state.dataset if first_outcome.state else None) or {}
        verification = outcome.verification or {}
        expect = case.expect

        def check(name: str, ok: bool, detail: str) -> None:
            checks[name] = ok
            if not ok:
                failures.append(detail)

        check(
            "status",
            outcome.status == expect.get("status"),
            f"status {outcome.status!r} != expected {expect.get('status')!r}",
        )
        expected_tools = set(expect.get("tools_used") or [])
        forbidden_tools = set(expect.get("tools_forbidden") or [])
        missing_tools = expected_tools - set(used_tools)
        present_forbidden = forbidden_tools & set(used_tools)
        check(
            "tools",
            not missing_tools and not present_forbidden,
            f"tool selection mismatch: missing={sorted(missing_tools)} forbidden={sorted(present_forbidden)}",
        )

        if expect.get("dataset_valid") is not None:
            check(
                "dataset_valid",
                bool(dataset_state.get("valid")) == bool(expect["dataset_valid"]),
                f"dataset.valid={dataset_state.get('valid')} != {expect['dataset_valid']}",
            )
        else:
            checks["dataset_valid"] = None

        if expect.get("dataset_analysis_ready") is not None:
            check(
                "dataset_analysis_ready",
                bool(dataset_state.get("analysis_ready")) == bool(expect["dataset_analysis_ready"]),
                "dataset.analysis_ready mismatch",
            )
        else:
            checks["dataset_analysis_ready"] = None

        checks["dataset"] = (
            None
            if checks.get("dataset_valid") is None
            and checks.get("dataset_analysis_ready") is None
            else bool(checks.get("dataset_valid") is not False and checks.get("dataset_analysis_ready") is not False)
        )

        expected_tasks = expect.get("tasks")
        if expected_tasks is not None:
            check(
                "tasks",
                len(tasks) == int(expected_tasks),
                f"tasks in DB {len(tasks)} != expected {expected_tasks}",
            )
        else:
            checks["tasks"] = None

        expected_verification = expect.get("verification_ok")
        if expected_verification is not None:
            check(
                "verification",
                bool(verification.get("ok")) == bool(expected_verification),
                f"verification.ok={verification.get('ok')} != {expected_verification}",
            )
        else:
            checks["verification"] = None

        if expect.get("approval_required") is not None:
            check(
                "approval",
                approval_requested == bool(expect["approval_required"]),
                f"approval_requested={approval_requested} != {expect['approval_required']}",
            )
        else:
            checks["approval"] = None

        for text in expect.get("final_contains") or []:
            check(
                f"final_contains[{text}]",
                text in final_answer,
                f"final answer does not contain {text!r}",
            )
        for text in expect.get("final_not_contains") or []:
            check(
                f"final_not_contains[{text}]",
                text not in final_answer,
                f"final answer unexpectedly contains {text!r}",
            )

        titles = [task.title for task in tasks]
        for text in expect.get("titles_not_contain") or []:
            check(
                f"title_not_contains[{text}]",
                all(text not in title for title in titles),
                f"a task title unexpectedly contains {text!r}",
            )

        if expect.get("retry_seen"):
            retried = any(
                trace.retry_count > 0 for trace in repos.traces.list_for_run(outcome.run_id)
            )
            check("retry_seen", retried, "no tool trace shows a retry")

        if expect.get("recovery"):
            check(
                "recovery",
                outcome.status == "COMPLETED" and len(tasks) > 0,
                "run did not recover to a completed, verified write",
            )
        else:
            checks["recovery"] = None

        if expect.get("duplicate") is not None:
            check(
                "duplicate",
                duplicate_flag == bool(expect["duplicate"]),
                f"duplicate={duplicate_flag} != {expect['duplicate']}",
            )
        else:
            checks["duplicate"] = None

        metric_failures: list[str] = []
        metric_checks = expect.get("metric_checks") or []
        trends = (first_outcome.state.trends if first_outcome.state else {}) or {}
        for item in metric_checks:
            metric = item["metric"]
            tolerance = float(item.get("tolerance", 0.02))
            trend = trends.get(metric)
            if not trend:
                metric_failures.append(f"{metric}: trend missing")
                continue
            expected_recent, expected_comparison = _independent_trend(
                FIXTURES_DIR / f"{case.dataset}.csv", metric
            )
            if abs(float(trend.get("recent_average", 0)) - expected_recent) > tolerance:
                metric_failures.append(
                    f"{metric}: recent {trend.get('recent_average')} != {expected_recent:.2f}"
                )
            if abs(float(trend.get("comparison_average", 0)) - expected_comparison) > tolerance:
                metric_failures.append(
                    f"{metric}: comparison {trend.get('comparison_average')} != {expected_comparison:.2f}"
                )
        if metric_checks:
            check("metric_accuracy", not metric_failures, "; ".join(metric_failures))
        else:
            checks["metric_accuracy"] = None

        if outcome.status == "WAITING_FOR_APPROVAL":
            harness.cancel(run_id, reason="evaluation finished without a decision")

        result.update(
            {
                "status": outcome.status,
                "expected_status": expect.get("status"),
                "tools_used": used_tools,
                "tool_calls": outcome.tool_calls,
                "steps": outcome.steps,
                "tasks_actual": len(tasks),
                "tasks_expected": expected_tasks,
                "verification_ok": verification.get("ok"),
                "verification_injected": bool(expect.get("verification_injected")),
                "approval_requested": approval_requested,
                "approval_granted": approval_granted,
                "duplicate": duplicate_flag,
                "tokens": dict(outcome.state.tokens) if outcome.state else {},
                "dataset_valid": dataset_state.get("valid"),
                "dataset_analysis_ready": dataset_state.get("analysis_ready"),
                "final_answer": final_answer,
                "checks": checks,
                "failures": failures,
            }
        )
    except Exception as exc:  # a crashing case is a failed case, never a silent skip
        result.update(
            {
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
                "checks": checks,
                "failures": failures + [f"exception: {type(exc).__name__}: {exc}"],
                "tools_used": [],
                "tasks_actual": 0,
                "verification_ok": None,
                "approval_requested": False,
                "approval_granted": False,
                "duplicate": False,
                "final_answer": "",
            }
        )
    finally:
        _cleanup_db(db_path)

    result["duration_ms"] = round((time.perf_counter() - start) * 1000, 1)
    applicable = [value for value in result.get("checks", {}).values() if value is not None]
    result["passed"] = bool(applicable) and all(applicable) and not result.get("failures")
    return result


def run_suite(
    *,
    repos: Repositories | None = None,
    limit: int | None = None,
    save_report: bool = True,
) -> EvalRecord:
    cases, min_days = load_cases(limit=limit)
    started = datetime.now(timezone.utc)
    results = [run_case(case, min_days) for case in cases]
    metrics = summarize(results)
    finished = datetime.now(timezone.utc)
    record = EvalRecord(
        eval_id=new_id("eval"),
        started_at=started.isoformat(timespec="milliseconds"),
        finished_at=finished.isoformat(timespec="milliseconds"),
        total=len(results),
        passed=sum(1 for item in results if item["passed"]),
        metrics=metrics,
        results=results,
    )

    if repos is not None:
        repos.evals.save(record)

    if save_report:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "latest.json").write_text(
            json.dumps(
                {
                    "eval_id": record.eval_id,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "total": record.total,
                    "passed": record.passed,
                    "metrics": record.metrics,
                    "results": record.results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (EVAL_DIR.parent / "docs").mkdir(parents=True, exist_ok=True)
        (EVAL_DIR.parent / "docs" / "evaluation_report.md").write_text(
            render_markdown(record), encoding="utf-8"
        )

    return record
