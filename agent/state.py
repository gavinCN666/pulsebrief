"""Serializable execution state for one agent run.

The run state is persisted in SQLite after every step. Conversation history is
deliberately *not* the source of truth: the harness can resume a paused run from
this structure alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from storage.models import RunStatus, utc_now_iso


def _default_tokens() -> dict[str, int]:
    return {"prompt": 0, "completion": 0}


@dataclass
class RunState:
    run_id: str
    user_id: str
    user_request: str
    dataset_id: str | None = None
    status: str = RunStatus.CREATED.value
    step_index: int = 0
    tool_call_count: int = 0
    started_at: str = field(default_factory=utc_now_iso)
    observations: list[dict[str, Any]] = field(default_factory=list)
    call_history: list[str] = field(default_factory=list)
    used_tools: list[str] = field(default_factory=list)

    dataset: dict[str, Any] | None = None
    trends: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    profile: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    validation_attempts: int = 0
    failed_metrics: list[str] = field(default_factory=list)

    pending_call: dict[str, Any] | None = None
    approval_id: str | None = None
    write_result: dict[str, Any] | None = None
    write_error: str | None = None
    verification: dict[str, Any] | None = None

    final_answer: str | None = None
    error: str | None = None
    tokens: dict[str, int] = field(default_factory=_default_tokens)
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "user_id": self.user_id,
            "user_request": self.user_request,
            "dataset_id": self.dataset_id,
            "status": self.status,
            "step_index": self.step_index,
            "tool_call_count": self.tool_call_count,
            "started_at": self.started_at,
            "observations": self.observations,
            "call_history": self.call_history,
            "used_tools": self.used_tools,
            "dataset": self.dataset,
            "trends": self.trends,
            "metrics": self.metrics,
            "profile": self.profile,
            "validation": self.validation,
            "validation_attempts": self.validation_attempts,
            "failed_metrics": self.failed_metrics,
            "pending_call": self.pending_call,
            "approval_id": self.approval_id,
            "write_result": self.write_result,
            "write_error": self.write_error,
            "verification": self.verification,
            "final_answer": self.final_answer,
            "error": self.error,
            "tokens": self.tokens,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        filtered.setdefault("tokens", _default_tokens())
        return cls(**filtered)
