"""Domain models shared by storage, tools and the agent harness."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CONSUMED = "CONSUMED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ActionType(str, Enum):
    SYSTEM = "SYSTEM"
    LLM = "LLM"
    TOOL = "TOOL"
    APPROVAL = "APPROVAL"
    VERIFICATION = "VERIFICATION"


class TraceStatus(str, Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    BLOCKED = "BLOCKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SKIPPED = "SKIPPED"


class DatasetStatus(str, Enum):
    READY = "READY"
    INVALID = "INVALID"
    INSUFFICIENT = "INSUFFICIENT"


class UserProfile(BaseModel):
    user_id: str
    display_name: str = ""
    goals: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    sleep_goal_hours: float = 7.5
    daily_steps_goal: int = 8000
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class DatasetRecord(BaseModel):
    dataset_id: str
    user_id: str
    filename: str
    status: DatasetStatus
    row_count: int
    start_date: str | None
    end_date: str | None
    validation: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)


class ActionTask(BaseModel):
    task_id: str
    user_id: str
    run_id: str
    dataset_id: str | None
    title: str
    description: str
    frequency: str
    duration_days: int
    scheduled_date: str
    status: TaskStatus = TaskStatus.PENDING
    idempotency_key: str
    task_index: int
    target_metric: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class AgentRun(BaseModel):
    run_id: str
    task_id: str | None
    user_id: str
    dataset_id: str | None
    user_request: str
    status: RunStatus
    error: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    completed_at: str | None = None
    tokens_prompt: int = 0
    tokens_completion: int = 0


class Approval(BaseModel):
    approval_id: str
    run_id: str
    tool_name: str
    payload_hash: str
    payload: dict[str, Any]
    status: ApprovalStatus
    reason: str | None = None
    requested_at: str = Field(default_factory=utc_now_iso)
    expires_at: str
    decided_at: str | None = None
    consumed_at: str | None = None
    decided_by: str | None = None


class ToolTrace(BaseModel):
    trace_id: str
    run_id: str
    step_index: int
    action_type: ActionType
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    output_summary: str = ""
    status: TraceStatus
    error: str | None = None
    duration_ms: float = 0.0
    retry_count: int = 0
    created_at: str = Field(default_factory=utc_now_iso)


class MemoryItem(BaseModel):
    memory_id: str
    user_id: str
    kind: str
    key: str
    value: Any
    source: str
    confidence: float = 1.0
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class EvalRecord(BaseModel):
    eval_id: str
    started_at: str
    finished_at: str | None
    total: int
    passed: int
    metrics: dict[str, Any]
    results: list[dict[str, Any]]
