"""Shared plan schemas for validation and task creation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from tools.base import StrictModel

MetricName = Literal[
    "sleep_hours",
    "steps",
    "active_minutes",
    "exercise_minutes",
    "resting_hr",
]


class PlanItem(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)
    frequency: Literal["daily", "weekly", "once"] = "daily"
    duration_days: int = Field(default=7, ge=1, le=30)
    estimated_minutes: int = Field(default=10, ge=0, le=180)
    target_metric: MetricName | None = None
    scheduled_date: str | None = Field(
        default=None,
        description="ISO date (YYYY-MM-DD). The validation tool fills a default when omitted.",
    )


class PlanItemOut(PlanItem):
    scheduled_date: str


class PlanIssue(StrictModel):
    code: str
    message: str
    item_index: int | None = None
    severity: Literal["error", "warning"] = "error"


class TaskOut(StrictModel):
    task_id: str
    title: str
    description: str
    frequency: str
    duration_days: int
    scheduled_date: str
    status: str
    target_metric: str | None = None
