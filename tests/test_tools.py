"""Tests for the six tools, independent of any LLM."""

from __future__ import annotations

import statistics

import pytest

from storage.validation import validate_csv_bytes
from tools.action_tools import (
    CreateActionTasksTool,
    batch_key,
    plan_payload,
)
from tools.base import ApprovalRequiredError, ToolStateError
from tools.dataset_tools import InspectDatasetTool
from tools.metrics_tools import QueryHealthMetricsTool
from tools.profile_tools import GetUserProfileTool
from tools.schemas import PlanItem
from tools.trend_tools import DetectTrendsTool
from tools.validation_tools import ValidateActionPlanTool

from agent.approvals import create_approval, decide_approval
from tests.helpers import BASE_PLAN, build_env, make_ctx


def test_validation_detects_bad_data():
    from tests.helpers import FIXTURES

    clean = validate_csv_bytes((FIXTURES / "clean_30d.csv").read_bytes())
    assert clean.valid and clean.analysis_ready and clean.row_count == 30

    negative = validate_csv_bytes((FIXTURES / "negative_values.csv").read_bytes())
    assert not negative.valid
    assert {issue.code for issue in negative.errors} >= {"out_of_range"}

    duplicate = validate_csv_bytes((FIXTURES / "duplicate_dates.csv").read_bytes())
    assert "duplicate_date" in {issue.code for issue in duplicate.errors}

    short = validate_csv_bytes((FIXTURES / "insufficient_7d.csv").read_bytes())
    assert short.valid and not short.analysis_ready
    assert "insufficient_data" in {issue.code for issue in short.issues}


def test_inspect_dataset(root):
    env = build_env(root, "clean_30d")
    out = InspectDatasetTool().run(make_ctx(env), {"dataset_id": env.dataset.dataset_id})
    assert out["valid"] is True
    assert out["analysis_ready"] is True
    assert out["row_count"] == 30
    assert out["date_range"] == {"start": "2026-10-11", "end": "2026-11-09"}
    assert set(out["available_metrics"]) == {
        "sleep_hours",
        "steps",
        "active_minutes",
        "exercise_minutes",
        "resting_hr",
    }
    assert out["error_count"] == 0


def test_inspect_dataset_reports_missing_column(root):
    env = build_env(root, "missing_exercise_column")
    out = InspectDatasetTool().run(make_ctx(env), {"dataset_id": env.dataset.dataset_id})
    assert out["valid"] is False
    assert out["analysis_ready"] is False
    assert "exercise_minutes" in out["missing_fields"]


def test_query_metric_aggregations_are_deterministic(root):
    env = build_env(root, "clean_30d")
    ctx = make_ctx(env)
    tool = QueryHealthMetricsTool()
    rows = env.repos.datasets.rows(env.dataset.dataset_id)
    values = [row["sleep_hours"] for row in rows]

    mean = tool.run(ctx, {"metric": "sleep_hours", "aggregation": "mean"})
    assert mean["value"] == round(statistics.mean(values), 2)

    median = tool.run(ctx, {"metric": "sleep_hours", "aggregation": "median"})
    assert median["value"] == round(statistics.median(values), 2)

    daily = tool.run(ctx, {"metric": "sleep_hours", "aggregation": "daily"})
    assert daily["count"] == 30
    assert len(daily["values"]) == 30

    weekly = tool.run(ctx, {"metric": "sleep_hours", "aggregation": "weekly"})
    # 30 days from Sunday 2026-10-11 through Monday 2026-11-09 touches 6 Monday-based weeks.
    assert len(weekly["values"]) == 6
    assert weekly["count"] == 30


def test_metric_query_blocked_for_invalid_dataset(root):
    env = build_env(root, "missing_sleep")
    with pytest.raises(ToolStateError):
        QueryHealthMetricsTool().run(
            make_ctx(env), {"metric": "sleep_hours", "aggregation": "mean"}
        )


def test_detect_trends_reports_direction(root):
    env = build_env(root, "clean_30d")
    out = DetectTrendsTool().run(make_ctx(env), {"metric": "sleep_hours"})
    assert out["data_sufficient"] is True
    assert out["trend"] == "decreasing"
    assert out["recent_average"] < out["comparison_average"]
    assert out["change"] < 0
    assert out["recent_points"] == 7


def test_detect_trends_insufficient_data(root):
    env = build_env(root, "insufficient_7d")
    out = DetectTrendsTool().run(make_ctx(env), {"metric": "sleep_hours"})
    assert out["data_sufficient"] is False
    assert out["trend"] == "insufficient_data"


def test_get_user_profile(root):
    env = build_env(root, "clean_30d")
    out = GetUserProfileTool().run(make_ctx(env), {"user_id": env.user_id})
    assert out["goals"] == ["improve_sleep", "increase_daily_activity"]
    assert out["available_time_per_day"] == 30
    assert out["exercise_intensity"] == "low"
    assert out["preferred_activity"] == ["walking"]


def test_validate_action_plan_accepts_compliant_plan(root):
    env = build_env(root, "clean_30d")
    out = ValidateActionPlanTool().run(make_ctx(env), {"plan": BASE_PLAN})
    assert out["valid"] is True
    assert len(out["normalized_plan"]) == 4
    assert all(item["scheduled_date"] == "2026-11-10" for item in out["normalized_plan"])
    assert out["checks"]["within_time_budget"] is True


def test_validate_action_plan_rejects_medical_claim(root):
    env = build_env(root, "clean_30d")
    plan = [dict(BASE_PLAN[0], description="每天服药治疗失眠，剂量遵医嘱。")]
    out = ValidateActionPlanTool().run(make_ctx(env), {"plan": plan})
    assert out["valid"] is False
    assert "medical_claim" in {issue["code"] for issue in out["issues"]}
    assert out["checks"]["no_medical_claims"] is False


def test_validate_action_plan_rejects_over_budget(root):
    env = build_env(root, "clean_30d")
    plan = [dict(BASE_PLAN[0], estimated_minutes=60)]
    out = ValidateActionPlanTool().run(make_ctx(env), {"plan": plan})
    assert out["valid"] is False
    assert "time_budget_exceeded" in {issue["code"] for issue in out["issues"]}


def test_validate_action_plan_respects_low_intensity(root):
    env = build_env(root, "clean_30d")
    plan = [dict(BASE_PLAN[1], title="每天跑步", description="每天进行高强度跑步训练。")]
    out = ValidateActionPlanTool().run(make_ctx(env), {"plan": plan})
    assert out["valid"] is False
    assert "preference_mismatch" in {issue["code"] for issue in out["issues"]}


def test_create_tasks_requires_approval(root):
    env = build_env(root, "clean_30d")
    ctx = make_ctx(env)
    validated = ValidateActionPlanTool().run(ctx, {"plan": BASE_PLAN})
    with pytest.raises(ApprovalRequiredError):
        CreateActionTasksTool().run(ctx, {"plan": validated["normalized_plan"]})


def test_create_tasks_with_approval_and_idempotency(root):
    env = build_env(root, "clean_30d")
    ctx = make_ctx(env)
    validated = ValidateActionPlanTool().run(ctx, {"plan": BASE_PLAN})
    plan = [PlanItem(**item) for item in validated["normalized_plan"]]
    payload = plan_payload(plan, env.dataset.dataset_id)
    approval = create_approval(
        env.repos,
        run_id=ctx.run_id,
        tool_name="create_action_tasks",
        payload=payload,
        ttl_minutes=30,
    )
    decide_approval(env.repos, approval.approval_id, approved=True)

    out = CreateActionTasksTool().run(
        ctx,
        {"plan": validated["normalized_plan"], "approval_id": approval.approval_id},
    )
    assert out["created"] is True
    assert out["task_count"] == 4
    assert len(env.repos.tasks.by_batch(batch_key(payload))) == 4
    assert env.repos.approvals.get(approval.approval_id).status.value == "CONSUMED"

    replay = CreateActionTasksTool().run(
        ctx,
        {"plan": validated["normalized_plan"], "approval_id": approval.approval_id},
    )
    assert replay["created"] is False
    assert replay["duplicate"] is True
    assert replay["task_count"] == 4
    assert len(env.repos.tasks.list_all(env.user_id)) == 4


def test_create_tasks_rejects_tampered_plan(root):
    env = build_env(root, "clean_30d")
    ctx = make_ctx(env)
    validated = ValidateActionPlanTool().run(ctx, {"plan": BASE_PLAN})
    plan = [PlanItem(**item) for item in validated["normalized_plan"]]
    approval = create_approval(
        env.repos,
        run_id=ctx.run_id,
        tool_name="create_action_tasks",
        payload=plan_payload(plan, env.dataset.dataset_id),
        ttl_minutes=30,
    )
    decide_approval(env.repos, approval.approval_id, approved=True)

    tampered = [dict(item) for item in validated["normalized_plan"]]
    tampered[0]["title"] = "被篡改的标题"
    with pytest.raises(ApprovalRequiredError):
        CreateActionTasksTool().run(
            ctx,
            {"plan": tampered, "approval_id": approval.approval_id},
        )
    assert len(env.repos.tasks.list_all(env.user_id)) == 0
