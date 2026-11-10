"""Shared test helpers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from config import HarnessConfig, ProviderSettings, Settings
from storage.models import AgentRun, RunStatus, new_id, utc_now_iso
from storage.repositories import Repositories
from storage.services import ensure_user, ingest_dataset_file
from tools.base import ToolContext

from agent.harness import AgentHarness
from agent.provider import BaseProvider, DeterministicPlanner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "data" / "fixtures"

BASE_PLAN = [
    {
        "title": "固定就寝时间",
        "description": "每天在固定时间前完成睡前准备，保持规律作息。",
        "frequency": "daily",
        "duration_days": 7,
        "estimated_minutes": 5,
        "target_metric": "sleep_hours",
    },
    {
        "title": "午后步行",
        "description": "午后安排一次轻松步行，以低强度方式增加每日步数。",
        "frequency": "daily",
        "duration_days": 7,
        "estimated_minutes": 10,
        "target_metric": "steps",
    },
    {
        "title": "睡前低强度拉伸",
        "description": "睡前进行低强度拉伸放松，缓解久坐带来的紧张感。",
        "frequency": "daily",
        "duration_days": 7,
        "estimated_minutes": 10,
        "target_metric": "exercise_minutes",
    },
    {
        "title": "记录睡眠与精力",
        "description": "每天记录实际睡眠时长和主观精力，为回顾提供依据。",
        "frequency": "daily",
        "duration_days": 7,
        "estimated_minutes": 5,
        "target_metric": "sleep_hours",
    },
]


@dataclass
class Env:
    root: Path
    settings: Settings
    repos: Repositories
    harness: AgentHarness
    user_id: str
    dataset: object
    report: object


def build_env(
    root: Path,
    fixture: str = "clean_30d",
    *,
    goals: list[str] | None = None,
    preferences: dict | None = None,
    available_time: int = 30,
    config_overrides: dict | None = None,
    faults: dict[str, int] | None = None,
    provider: BaseProvider | None = None,
) -> Env:
    db_path = root / f"{uuid.uuid4().hex}.db"
    prefs = {
        "exercise_intensity": "low",
        "preferred_activity": ["walking"],
        "available_time_per_day": available_time,
    }
    if preferences:
        prefs.update(preferences)
    repos = Repositories.open(str(db_path))
    user_id = "test_user"
    ensure_user(
        repos,
        user_id,
        display_name="Test User",
        goals=goals or ["improve_sleep", "increase_daily_activity"],
        preferences=prefs,
    )
    dataset, report = ingest_dataset_file(
        repos, user_id=user_id, path=FIXTURES / f"{fixture}.csv"
    )
    settings = Settings(
        harness=HarnessConfig(db_path=str(db_path), **(config_overrides or {})),
        provider=ProviderSettings(provider="mock"),
    )
    harness = AgentHarness(
        settings=settings,
        repos=repos,
        provider=provider or DeterministicPlanner(),
        fault_injection=dict(faults or {}),
    )
    return Env(
        root=root,
        settings=settings,
        repos=repos,
        harness=harness,
        user_id=user_id,
        dataset=dataset,
        report=report,
    )


def new_run(repos: Repositories, user_id: str, dataset_id: str) -> str:
    run_id = new_id("run")
    repos.runs.create(
        AgentRun(
            run_id=run_id,
            task_id=None,
            user_id=user_id,
            dataset_id=dataset_id,
            user_request="test run",
            status=RunStatus.RUNNING,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
    )
    return run_id


def make_ctx(env: Env, run_id: str | None = None) -> ToolContext:
    return ToolContext(
        repos=env.repos,
        config=env.settings.harness,
        user_id=env.user_id,
        dataset_id=env.dataset.dataset_id,
        run_id=run_id or new_run(env.repos, env.user_id, env.dataset.dataset_id),
    )
