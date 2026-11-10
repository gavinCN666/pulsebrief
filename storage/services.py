"""Application services shared by the UI, tests and the evaluator."""

from __future__ import annotations

import hashlib
from pathlib import Path

from config import DATA_DIR
from storage.models import (
    DatasetRecord,
    DatasetStatus,
    UserProfile,
    new_id,
    utc_now_iso,
)
from storage.repositories import Repositories
from storage.validation import ValidationReport, validate_csv_bytes


def ensure_user(
    repos: Repositories,
    user_id: str,
    *,
    display_name: str = "",
    goals: list[str] | None = None,
    preferences: dict | None = None,
    sleep_goal_hours: float = 7.5,
    daily_steps_goal: int = 8000,
    overwrite: bool = False,
) -> UserProfile:
    existing = repos.users.get(user_id)
    if existing is not None and not overwrite:
        return existing
    profile = UserProfile(
        user_id=user_id,
        display_name=display_name or (existing.display_name if existing else ""),
        goals=list(goals if goals is not None else (existing.goals if existing else [])),
        preferences=dict(
            preferences if preferences is not None else (existing.preferences if existing else {})
        ),
        sleep_goal_hours=sleep_goal_hours,
        daily_steps_goal=daily_steps_goal,
        created_at=existing.created_at if existing else utc_now_iso(),
        updated_at=utc_now_iso(),
    )
    return repos.users.upsert(profile)


def ingest_dataset_csv(
    repos: Repositories,
    *,
    user_id: str,
    filename: str,
    content: bytes,
    min_days: int = 14,
) -> tuple[DatasetRecord, ValidationReport]:
    if repos.users.get(user_id) is None:
        raise ValueError(f"User {user_id!r} does not exist.")

    report = validate_csv_bytes(content, min_days=min_days)
    dataset = DatasetRecord(
        dataset_id=new_id("ds"),
        user_id=user_id,
        filename=filename,
        status=DatasetStatus(report.status),
        row_count=report.row_count,
        start_date=report.start_date,
        end_date=report.end_date,
        validation=report.to_dict(),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    repos.datasets.create(dataset, report.clean_rows)
    return dataset, report


def ingest_dataset_file(
    repos: Repositories,
    *,
    user_id: str,
    path: str | Path,
    min_days: int = 14,
) -> tuple[DatasetRecord, ValidationReport]:
    file_path = Path(path)
    return ingest_dataset_csv(
        repos,
        user_id=user_id,
        filename=file_path.name,
        content=file_path.read_bytes(),
        min_days=min_days,
    )


def load_sample_dataset(repos: Repositories, user_id: str, min_days: int = 14):
    return ingest_dataset_file(
        repos,
        user_id=user_id,
        path=DATA_DIR / "sample_health_data.csv",
        min_days=min_days,
    )
