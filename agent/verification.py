"""Environment verification.

Never trust the model's claim. Verification re-reads the database and checks
row count, task identity, required fields and approval consumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from storage.models import ApprovalStatus
from storage.repositories import Repositories


@dataclass
class VerificationCheck:
    code: str
    ok: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "ok": self.ok, "message": self.message}


@dataclass
class VerificationResult:
    ok: bool
    checks: list[VerificationCheck]
    expected_count: int
    actual_count: int
    task_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "task_ids": self.task_ids,
        }


def verify_task_creation(
    repos: Repositories,
    *,
    run_id: str,
    expected_task_ids: list[str],
    approval_id: str | None,
    batch_key: str | None = None,
    duplicate: bool = False,
) -> VerificationResult:
    tasks = (
        repos.tasks.by_batch(batch_key)
        if batch_key
        else repos.tasks.by_run(run_id)
    )
    actual_ids = [task.task_id for task in tasks]
    checks: list[VerificationCheck] = []

    checks.append(
        VerificationCheck(
            "count_matches",
            len(tasks) == len(expected_task_ids),
            f"Expected {len(expected_task_ids)} tasks in SQLite, found {len(tasks)}.",
        )
    )
    checks.append(
        VerificationCheck(
            "ids_match",
            set(expected_task_ids).issubset(set(actual_ids)),
            "Every task id returned by the write tool exists in SQLite.",
        )
    )
    checks.append(
        VerificationCheck(
            "required_fields",
            all(t.title.strip() and t.description.strip() and t.scheduled_date for t in tasks),
            "All tasks have title, description and scheduled_date.",
        )
    )
    checks.append(
        VerificationCheck(
            "valid_schedule_dates",
            all(_is_iso_date(t.scheduled_date) for t in tasks),
            "All scheduled dates are valid ISO dates.",
        )
    )
    checks.append(
        VerificationCheck(
            "status_pending",
            all(t.status.value == "PENDING" for t in tasks),
            "Newly created tasks are in PENDING state.",
        )
    )
    if duplicate:
        checks.append(
            VerificationCheck(
                "run_link",
                all(t.run_id != "" for t in tasks),
                "Idempotent replay points at previously persisted tasks.",
            )
        )
    else:
        checks.append(
            VerificationCheck(
                "run_link",
                all(t.run_id == run_id for t in tasks),
                "All tasks are linked to this agent run.",
            )
        )

    if approval_id:
        approval = repos.approvals.get(approval_id)
        if duplicate:
            ok = approval is not None and approval.status in {
                ApprovalStatus.APPROVED,
                ApprovalStatus.CONSUMED,
            }
            message = "A matching approval exists for the idempotent replay."
        else:
            ok = approval is not None and approval.status == ApprovalStatus.CONSUMED
            message = "The one-time approval was consumed by the write."
        checks.append(VerificationCheck("approval_consumed", ok, message))

    return VerificationResult(
        ok=all(check.ok for check in checks),
        checks=checks,
        expected_count=len(expected_task_ids),
        actual_count=len(tasks),
        task_ids=actual_ids,
    )


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False
