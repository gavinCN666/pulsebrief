"""Tool 2 — query_health_metrics.

All arithmetic is deterministic Python/pandas work; the LLM never computes a
metric itself.
"""

from __future__ import annotations

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
Aggregation = Literal["daily", "weekly", "mean", "median", "min", "max"]


class QueryHealthMetricsInput(StrictModel):
    metric: MetricName
    start_date: str | None = Field(default=None, description="Inclusive start date (YYYY-MM-DD).")
    end_date: str | None = Field(default=None, description="Inclusive end date (YYYY-MM-DD).")
    aggregation: Aggregation = "daily"


class MetricPoint(StrictModel):
    date: str
    value: float


class QueryHealthMetricsOutput(StrictModel):
    dataset_id: str
    metric: str
    unit: str
    aggregation: str
    start_date: str
    end_date: str
    count: int
    missing_days: int
    values: list[MetricPoint] = Field(default_factory=list)
    value: float | None = None
    data_sufficient: bool = True
    note: str = ""


class QueryHealthMetricsTool(BaseTool):
    name = "query_health_metrics"
    description = (
        "Query one health metric over a date range and aggregate it deterministically. "
        "Supports daily points, weekly means, or scalar mean/median/min/max. "
        "Use this instead of estimating numbers from the raw file."
    )
    category = "analysis"
    input_model = QueryHealthMetricsInput
    output_model = QueryHealthMetricsOutput

    def execute(self, ctx: ToolContext, payload: QueryHealthMetricsInput) -> QueryHealthMetricsOutput:
        dataset = get_dataset_or_raise(ctx.repos, ctx.dataset_id)
        require_valid_dataset(dataset)

        frame = ctx.repos.datasets.dataframe(dataset.dataset_id)
        if frame.empty:
            raise ToolStateError("The dataset has no stored rows.")

        frame = frame.dropna(subset=[payload.metric]).copy()
        if frame.empty:
            raise ToolStateError(f"Metric {payload.metric} has no values in this dataset.")

        frame["date"] = pd.to_datetime(frame["date"])
        available_start = frame["date"].min().date()
        available_end = frame["date"].max().date()
        start = parse_iso_date(payload.start_date, "start_date") if payload.start_date else available_start
        end = parse_iso_date(payload.end_date, "end_date") if payload.end_date else available_end
        if start > end:
            raise ToolStateError(f"start_date {start} is after end_date {end}.")

        subset = frame[(frame["date"].dt.date >= start) & (frame["date"].dt.date <= end)]
        if subset.empty:
            raise ToolStateError(
                f"No {payload.metric} records exist between {start} and {end}. "
                f"Available range is {available_start} to {available_end}."
            )

        unit = METRIC_UNITS[payload.metric]
        missing_days = max(0, (end - start).days + 1 - int(subset["date"].dt.normalize().nunique()))
        values: list[MetricPoint] = []
        scalar: float | None = None
        note = ""

        if payload.aggregation == "daily":
            ordered = subset.sort_values("date")
            values = [
                MetricPoint(date=row_date.date().isoformat(), value=round(float(value), 2))
                for row_date, value in ordered[["date", payload.metric]].itertuples(
                    index=False, name=None
                )
            ]
            note = f"{len(values)} daily values; missing calendar days are reported separately."
        elif payload.aggregation == "weekly":
            subset["week_start"] = subset["date"] - pd.to_timedelta(subset["date"].dt.weekday, unit="D")
            grouped = subset.groupby("week_start")[payload.metric].mean().sort_index()
            values = [
                MetricPoint(date=week_start.date().isoformat(), value=round(float(value), 2))
                for week_start, value in grouped.items()
            ]
            note = "Weekly points are means per calendar week (Monday start)."
        else:
            series = subset[payload.metric].astype(float)
            scalar = float(getattr(series, payload.aggregation)())
            scalar = round(scalar, 2)
            note = f"{payload.aggregation} over {len(series)} values."

        count = int(len(subset))
        return QueryHealthMetricsOutput(
            dataset_id=dataset.dataset_id,
            metric=payload.metric,
            unit=unit,
            aggregation=payload.aggregation,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            count=count,
            missing_days=missing_days,
            values=values,
            value=scalar,
            data_sufficient=count >= 3,
            note=note,
        )
