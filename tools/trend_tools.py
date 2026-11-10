"""Tool 3 — detect_trends.

Compares a recent window with the preceding window. Insufficient coverage is
reported explicitly instead of being smoothed into a fake conclusion.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

import pandas as pd
from pydantic import Field

from config import METRIC_UNITS
from tools.base import BaseTool, StrictModel, ToolContext, ToolStateError
from tools.common import get_dataset_or_raise, parse_iso_date, require_valid_dataset

MetricName = Literal[
    "sleep_hours",
    "steps",
    "active_minutes",
    "exercise_minutes",
    "resting_hr",
]


class DetectTrendsInput(StrictModel):
    metric: MetricName
    recent_days: int = Field(default=7, ge=1, le=60)
    comparison_days: int = Field(default=7, ge=1, le=60)
    end_date: str | None = Field(default=None, description="Defaults to the latest date in the dataset.")


class DetectTrendsOutput(StrictModel):
    metric: str
    unit: str
    recent_average: float | None = None
    comparison_average: float | None = None
    change: float | None = None
    change_pct: float | None = None
    trend: Literal["increasing", "decreasing", "stable", "insufficient_data"]
    data_sufficient: bool
    recent_days: int
    comparison_days: int
    recent_start: str | None = None
    recent_end: str | None = None
    comparison_start: str | None = None
    comparison_end: str | None = None
    recent_points: int = 0
    comparison_points: int = 0
    note: str = ""


class DetectTrendsTool(BaseTool):
    name = "detect_trends"
    description = (
        "Compare a recent time window against the preceding window for one metric. "
        "Returns averages, absolute/relative change and increasing/decreasing/stable. "
        "If data coverage is too low it returns trend='insufficient_data' instead of guessing."
    )
    category = "analysis"
    input_model = DetectTrendsInput
    output_model = DetectTrendsOutput

    def execute(self, ctx: ToolContext, payload: DetectTrendsInput) -> DetectTrendsOutput:
        dataset = get_dataset_or_raise(ctx.repos, ctx.dataset_id)
        require_valid_dataset(dataset)

        frame = ctx.repos.datasets.dataframe(dataset.dataset_id)
        frame = frame.dropna(subset=[payload.metric]).copy()
        if frame.empty:
            raise ToolStateError(f"Metric {payload.metric} has no values in this dataset.")
        frame["date"] = pd.to_datetime(frame["date"])

        end = (
            parse_iso_date(payload.end_date, "end_date")
            if payload.end_date
            else frame["date"].max().date()
        )
        recent_start = end - timedelta(days=payload.recent_days - 1)
        comparison_end = recent_start - timedelta(days=1)
        comparison_start = comparison_end - timedelta(days=payload.comparison_days - 1)

        recent = frame[
            (frame["date"].dt.date >= recent_start) & (frame["date"].dt.date <= end)
        ][payload.metric]
        comparison = frame[
            (frame["date"].dt.date >= comparison_start)
            & (frame["date"].dt.date <= comparison_end)
        ][payload.metric]

        def coverage(count: int, days: int) -> bool:
            return count >= 3 and count / days >= 0.6

        sufficient = coverage(len(recent), payload.recent_days) and coverage(
            len(comparison), payload.comparison_days
        )
        unit = METRIC_UNITS[payload.metric]

        if not sufficient:
            return DetectTrendsOutput(
                metric=payload.metric,
                unit=unit,
                trend="insufficient_data",
                data_sufficient=False,
                recent_days=payload.recent_days,
                comparison_days=payload.comparison_days,
                recent_start=recent_start.isoformat(),
                recent_end=end.isoformat(),
                comparison_start=comparison_start.isoformat(),
                comparison_end=comparison_end.isoformat(),
                recent_points=int(len(recent)),
                comparison_points=int(len(comparison)),
                note=(
                    f"数据不足：最近窗口 {len(recent)} 个有效值，对照窗口 {len(comparison)} 个；"
                    "无法给出可靠趋势。"
                ),
            )

        recent_avg = float(recent.mean())
        comparison_avg = float(comparison.mean())
        change = recent_avg - comparison_avg
        change_pct = (change / comparison_avg * 100.0) if comparison_avg else 0.0
        if abs(change_pct) < 3.0:
            trend = "stable"
        elif change > 0:
            trend = "increasing"
        else:
            trend = "decreasing"

        return DetectTrendsOutput(
            metric=payload.metric,
            unit=unit,
            recent_average=round(recent_avg, 2),
            comparison_average=round(comparison_avg, 2),
            change=round(change, 2),
            change_pct=round(change_pct, 1),
            trend=trend,
            data_sufficient=True,
            recent_days=payload.recent_days,
            comparison_days=payload.comparison_days,
            recent_start=recent_start.isoformat(),
            recent_end=end.isoformat(),
            comparison_start=comparison_start.isoformat(),
            comparison_end=comparison_end.isoformat(),
            recent_points=int(len(recent)),
            comparison_points=int(len(comparison)),
            note=(
                "最近窗口与此前窗口的统计比较；这是相关性观察，不代表因果关系，"
                "也不构成医学结论。"
            ),
        )
