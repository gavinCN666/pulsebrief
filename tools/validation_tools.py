"""Tool 5 — validate_action_plan.

Deterministic guardrails in front of the only write tool. The LLM proposes a
plan; this tool decides whether it is admissible given the user's profile.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal

from pydantic import Field

from tools.base import BaseTool, StrictModel, ToolContext, ToolInputError
from tools.common import get_dataset_or_raise, parse_iso_date
from tools.schemas import PlanIssue, PlanItem, PlanItemOut

MEDICAL_PATTERN = re.compile(
    r"诊断|治疗|用药|服药|处方|药物|治愈|病症|开药|停药|"
    r"diagnos|prescri|medicat|dosage|\bcure\b|therapy",
    re.IGNORECASE,
)
HIGH_INTENSITY_PATTERN = re.compile(
    r"高强度|剧烈|冲刺|极限训练|力量训练|大重量|跑步|长跑|马拉松|"
    r"\bHIIT\b|\bsprint|marathon|heavy lifting",
    re.IGNORECASE,
)
GOAL_METRICS = {
    "improve_sleep": {"sleep_hours"},
    "increase_daily_activity": {"steps", "active_minutes"},
    "reduce_resting_hr": {"resting_hr", "exercise_minutes"},
}


class ValidateActionPlanInput(StrictModel):
    plan: list[PlanItem] = Field(min_length=1, max_length=10)


class ValidateActionPlanOutput(StrictModel):
    valid: bool
    issues: list[PlanIssue] = Field(default_factory=list)
    normalized_plan: list[PlanItemOut] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    available_time_per_day: int = 30
    summary: str = ""


class ValidateActionPlanTool(BaseTool):
    name = "validate_action_plan"
    description = (
        "Deterministically validate a proposed 7-day action plan against the user's available "
        "time, exercise preferences and safety rules. Returns normalized items with concrete "
        "start dates. A plan must pass here before create_action_tasks is allowed."
    )
    category = "analysis"
    input_model = ValidateActionPlanInput
    output_model = ValidateActionPlanOutput

    def execute(self, ctx: ToolContext, payload: ValidateActionPlanInput) -> ValidateActionPlanOutput:
        profile = ctx.repos.users.get(ctx.user_id)
        if profile is None:
            raise ToolInputError(f"User {ctx.user_id!r} does not exist; cannot validate a plan.")

        preferences = profile.preferences or {}
        try:
            available = int(preferences.get("available_time_per_day", 30))
        except (TypeError, ValueError):
            available = 30
        intensity = str(preferences.get("exercise_intensity", "low")).lower()
        preferred = preferences.get("preferred_activity", [])
        if isinstance(preferred, str):
            preferred = [preferred]
        preferred = [str(p).lower() for p in preferred]

        default_start = self._default_start(ctx)
        issues: list[PlanIssue] = []
        normalized: list[PlanItemOut] = []

        if len(payload.plan) > ctx.config.plan_max_tasks:
            issues.append(
                PlanIssue(
                    code="too_many_tasks",
                    message=f"计划包含 {len(payload.plan)} 项任务，最多允许 {ctx.config.plan_max_tasks} 项。",
                )
            )
        if available <= 0:
            issues.append(
                PlanIssue(
                    code="invalid_time_budget",
                    message="用户每日可用时间为 0，无法安排行动计划。",
                )
            )

        daily_load = 0.0
        for index, item in enumerate(payload.plan):
            text = f"{item.title} {item.description}"
            scheduled = self._normalize_date(item, default_start, index, issues)
            duration = 1 if item.frequency == "once" else item.duration_days
            normalized.append(
                PlanItemOut(
                    **item.model_dump(exclude={"scheduled_date", "duration_days"}),
                    duration_days=duration,
                    scheduled_date=scheduled,
                )
            )

            if len(item.title.strip()) < 4 or len(item.description.strip()) < 8:
                issues.append(
                    PlanIssue(
                        code="goal_unclear",
                        message=f"第 {index + 1} 项任务缺少清晰的目标描述。",
                        item_index=index,
                    )
                )
            if item.duration_days > ctx.config.plan_max_duration_days:
                issues.append(
                    PlanIssue(
                        code="duration_too_long",
                        message=(
                            f"第 {index + 1} 项任务持续 {item.duration_days} 天，超过 "
                            f"{ctx.config.plan_max_duration_days} 天上限。"
                        ),
                        item_index=index,
                    )
                )
            if item.estimated_minutes > available:
                issues.append(
                    PlanIssue(
                        code="item_exceeds_daily_time",
                        message=(
                            f"第 {index + 1} 项任务需要 {item.estimated_minutes} 分钟，"
                            f"超过用户每天 {available} 分钟的可用时间。"
                        ),
                        item_index=index,
                    )
                )
            if MEDICAL_PATTERN.search(text):
                issues.append(
                    PlanIssue(
                        code="medical_claim",
                        message=(
                            f"第 {index + 1} 项任务包含医疗诊断或治疗表述，"
                            "行动计划只能提供一般生活习惯建议。"
                        ),
                        item_index=index,
                    )
                )
            if intensity == "low" and HIGH_INTENSITY_PATTERN.search(text):
                issues.append(
                    PlanIssue(
                        code="preference_mismatch",
                        message=(
                            f"第 {index + 1} 项任务包含高强度活动，"
                            "不符合用户选择的低强度运动偏好。"
                        ),
                        item_index=index,
                    )
                )
            elif preferred and any(
                word in text.lower() for word in ("跑步", "骑行", "游泳", "hiit", "run", "cycling")
            ) and not any(word in " ".join(preferred) for word in ("跑步", "骑行")):
                issues.append(
                    PlanIssue(
                        code="preference_mismatch",
                        message=(
                            f"第 {index + 1} 项任务的活动类型可能不符合用户偏好"
                            f"（{', '.join(preferred)}）。"
                        ),
                        item_index=index,
                        severity="warning",
                    )
                )

            if item.frequency == "daily":
                daily_load += item.estimated_minutes
            elif item.frequency == "weekly":
                daily_load += item.estimated_minutes / 7.0
            else:
                daily_load += item.estimated_minutes / 7.0

        if daily_load > available + 0.01:
            issues.append(
                PlanIssue(
                    code="time_budget_exceeded",
                    message=(
                        f"计划平均每天需要 {daily_load:.0f} 分钟，"
                        f"超过用户每天 {available} 分钟的可用时间。"
                    ),
                )
            )

        goal_metrics: set[str] = set()
        for goal in profile.goals:
            goal_metrics |= GOAL_METRICS.get(goal, set())
        targets = {item.target_metric for item in normalized if item.target_metric}
        if goal_metrics and not (targets & goal_metrics):
            issues.append(
                PlanIssue(
                    code="goal_not_addressed",
                    message="计划没有直接覆盖用户设定的目标指标。",
                    severity="warning",
                )
            )

        checks = {
            "has_clear_goal": not any(i.code == "goal_unclear" for i in issues),
            "within_time_budget": not any(
                i.code in {"time_budget_exceeded", "item_exceeds_daily_time"} for i in issues
            ),
            "matches_preferences": not any(
                i.code == "preference_mismatch" and i.severity == "error" for i in issues
            ),
            "no_medical_claims": not any(i.code == "medical_claim" for i in issues),
            "dates_complete": not any(i.code == "invalid_schedule_date" for i in issues),
            "within_task_limit": not any(i.code == "too_many_tasks" for i in issues),
        }
        errors = [i for i in issues if i.severity == "error"]
        valid = not errors

        if valid:
            summary = (
                f"计划校验通过：{len(normalized)} 项任务，平均每天约 {daily_load:.0f} 分钟，"
                f"符合用户每天 {available} 分钟的时间约束。"
            )
        else:
            summary = f"计划校验未通过：{len(errors)} 个错误，需要修正后再提交审批。"

        return ValidateActionPlanOutput(
            valid=valid,
            issues=issues,
            normalized_plan=normalized,
            checks=checks,
            available_time_per_day=available,
            summary=summary,
        )

    def _default_start(self, ctx: ToolContext) -> date:
        if ctx.dataset_id:
            dataset = get_dataset_or_raise(ctx.repos, ctx.dataset_id)
            if dataset.end_date:
                try:
                    return date.fromisoformat(dataset.end_date) + timedelta(days=1)
                except ValueError:
                    pass
        return date.today() + timedelta(days=1)

    def _normalize_date(
        self, item: PlanItem, default_start: date, index: int, issues: list[PlanIssue]
    ) -> str:
        if not item.scheduled_date:
            return default_start.isoformat()
        try:
            scheduled = parse_iso_date(item.scheduled_date, "scheduled_date")
        except ToolInputError:
            issues.append(
                PlanIssue(
                    code="invalid_schedule_date",
                    message=f"第 {index + 1} 项任务的执行日期格式无效。",
                    item_index=index,
                )
            )
            return default_start.isoformat()
        lower = default_start - timedelta(days=7)
        upper = default_start + timedelta(days=30)
        if scheduled < lower or scheduled > upper:
            issues.append(
                PlanIssue(
                    code="invalid_schedule_date",
                    message=(
                        f"第 {index + 1} 项任务的日期 {scheduled.isoformat()} 超出允许范围"
                        f"（{lower.isoformat()} ~ {upper.isoformat()}）。"
                    ),
                    item_index=index,
                )
            )
            return default_start.isoformat()
        return scheduled.isoformat()
