"""Small helpers shared by tools."""

from __future__ import annotations

from datetime import date, datetime

from storage.models import DatasetRecord
from storage.repositories import Repositories
from tools.base import ToolInputError, ToolNotFoundError, ToolStateError


def get_dataset_or_raise(repos: Repositories, dataset_id: str | None) -> DatasetRecord:
    if not dataset_id:
        raise ToolInputError("No dataset is attached to this run.")
    dataset = repos.datasets.get(dataset_id)
    if dataset is None:
        raise ToolNotFoundError(f"Dataset {dataset_id!r} does not exist.")
    return dataset


def require_valid_dataset(dataset: DatasetRecord) -> None:
    validation = dataset.validation or {}
    if not validation.get("valid", False):
        raise ToolStateError(
            f"Dataset {dataset.dataset_id} failed validation; metric queries are blocked. "
            "Call inspect_dataset to review the issues before analyzing.",
            details={"status": dataset.status.value, "issues": validation.get("errors", [])[:5]},
        )


def parse_iso_date(value: str, field_name: str = "date") -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ToolInputError(f"{field_name} must be a YYYY-MM-DD date, got {value!r}.") from exc


def round_half(value: float, digits: int = 2) -> float:
    return float(round(value, digits))


def metric_display(metric: str) -> str:
    return {
        "sleep_hours": "睡眠时长",
        "steps": "每日步数",
        "active_minutes": "活动时长",
        "exercise_minutes": "运动时长",
        "resting_hr": "静息心率",
    }.get(metric, metric)
