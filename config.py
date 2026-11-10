"""Central configuration for PulseBrief.

All tunable harness limits live here instead of being scattered across business
code, as required by the PRD (section 4 of the implementation constraints).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = BASE_DIR / "docs"
RESULTS_DIR = BASE_DIR / "evaluation" / "results"

load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # fail loudly instead of silently using the default
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class HarnessConfig:
    """Execution limits and safety policy knobs."""

    max_steps: int = 12
    max_tool_retries: int = 1
    tool_timeout_seconds: float = 10.0
    max_concurrent_runs: int = 1
    max_tool_calls_per_run: int = 24
    max_identical_tool_calls: int = 2
    max_run_seconds: float = 180.0
    approval_ttl_minutes: int = 30
    recent_tool_results_in_context: int = 5
    max_context_chars: int = 14_000
    min_analysis_days: int = 14
    plan_max_tasks: int = 7
    plan_max_duration_days: int = 14
    db_path: str = str(BASE_DIR / "pulsebrief.db")

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        return cls(
            max_steps=_env_int("HARNESS_MAX_STEPS", cls.max_steps),
            max_tool_retries=_env_int("HARNESS_MAX_TOOL_RETRIES", cls.max_tool_retries),
            tool_timeout_seconds=_env_float("HARNESS_TOOL_TIMEOUT_SECONDS", cls.tool_timeout_seconds),
            max_concurrent_runs=_env_int("HARNESS_MAX_CONCURRENT_RUNS", cls.max_concurrent_runs),
            max_tool_calls_per_run=_env_int("HARNESS_MAX_TOOL_CALLS", cls.max_tool_calls_per_run),
            max_identical_tool_calls=_env_int(
                "HARNESS_MAX_IDENTICAL_TOOL_CALLS", cls.max_identical_tool_calls
            ),
            max_run_seconds=_env_float("HARNESS_MAX_RUN_SECONDS", cls.max_run_seconds),
            approval_ttl_minutes=_env_int("HARNESS_APPROVAL_TTL_MINUTES", cls.approval_ttl_minutes),
            recent_tool_results_in_context=_env_int(
                "HARNESS_RECENT_TOOL_RESULTS", cls.recent_tool_results_in_context
            ),
            max_context_chars=_env_int("HARNESS_MAX_CONTEXT_CHARS", cls.max_context_chars),
            min_analysis_days=_env_int("HARNESS_MIN_ANALYSIS_DAYS", cls.min_analysis_days),
            db_path=os.getenv("PULSEBRIEF_DB", cls.db_path),
        )


@dataclass(frozen=True)
class ProviderSettings:
    """Model-provider configuration; the runtime only sees the adapter interface."""

    provider: str = "mock"
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1600
    timeout_seconds: float = 60.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "ProviderSettings":
        return cls(
            provider=os.getenv("LLM_PROVIDER", cls.provider).strip().lower(),
            model=os.getenv("LLM_MODEL", cls.model),
            base_url=os.getenv("LLM_BASE_URL", cls.base_url).rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", cls.api_key),
            temperature=_env_float("LLM_TEMPERATURE", cls.temperature),
            max_tokens=_env_int("LLM_MAX_TOKENS", cls.max_tokens),
            timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", cls.timeout_seconds),
            max_retries=_env_int("LLM_MAX_RETRIES", cls.max_retries),
        )

    @property
    def is_live_model(self) -> bool:
        return self.provider not in {"mock", "offline", "deterministic"}


@dataclass(frozen=True)
class Settings:
    harness: HarnessConfig = field(default_factory=HarnessConfig.from_env)
    provider: ProviderSettings = field(default_factory=ProviderSettings.from_env)
    app_title: str = "PulseBrief"
    demo_user_id: str = "demo_user_001"

    @classmethod
    def load(cls) -> "Settings":
        return cls()


DEFAULT_GOALS = ["improve_sleep", "increase_daily_activity"]
DEFAULT_PREFERENCES = {
    "exercise_intensity": "low",
    "preferred_activity": ["walking"],
    "available_time_per_day": 30,
}
DEFAULT_SLEEP_GOAL_HOURS = 7.5
DEFAULT_DAILY_STEPS_GOAL = 8000

SUPPORTED_METRICS = [
    "sleep_hours",
    "steps",
    "active_minutes",
    "exercise_minutes",
    "resting_hr",
]

METRIC_UNITS = {
    "sleep_hours": "hours",
    "steps": "steps",
    "active_minutes": "minutes",
    "exercise_minutes": "minutes",
    "resting_hr": "bpm",
}
