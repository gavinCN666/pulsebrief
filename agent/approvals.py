"""Human-in-the-loop approvals, bound to run + exact write payload."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from storage.models import Approval, ApprovalStatus, new_id, utc_now, utc_now_iso
from storage.repositories import Repositories
from tools.action_tools import payload_hash


def create_approval(
    repos: Repositories,
    *,
    run_id: str,
    tool_name: str,
    payload: dict[str, Any],
    ttl_minutes: int,
    reason: str | None = None,
) -> Approval:
    now = utc_now()
    approval = Approval(
        approval_id=new_id("apr"),
        run_id=run_id,
        tool_name=tool_name,
        payload_hash=payload_hash(payload),
        payload=payload,
        status=ApprovalStatus.PENDING,
        reason=reason or "Write operation requires explicit user approval",
        requested_at=now.isoformat(timespec="milliseconds"),
        expires_at=(now + timedelta(minutes=ttl_minutes)).isoformat(timespec="milliseconds"),
    )
    repos.approvals.create(approval)
    return approval


def decide_approval(
    repos: Repositories,
    approval_id: str,
    *,
    approved: bool,
    reason: str | None = None,
    decided_by: str = "user",
) -> Approval:
    approval = repos.approvals.get(approval_id)
    if approval is None:
        raise ValueError(f"Approval {approval_id!r} does not exist.")
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError(f"Approval is already {approval.status.value}.")

    expires_at = datetime.fromisoformat(approval.expires_at)
    if expires_at <= utc_now():
        repos.approvals.set_status(approval.approval_id, ApprovalStatus.EXPIRED, reason="TTL elapsed")
        raise ValueError("Approval has expired; request a new one.")

    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    repos.approvals.set_status(
        approval.approval_id,
        status,
        reason=reason,
        decided_by=decided_by,
    )
    refreshed = repos.approvals.get(approval.approval_id)
    assert refreshed is not None
    return refreshed


def pending_payload_matches(approval: Approval, payload: dict[str, Any]) -> bool:
    return approval.payload_hash == payload_hash(payload)


def utcnow_iso() -> str:
    return utc_now_iso()


__all__ = [
    "create_approval",
    "decide_approval",
    "pending_payload_matches",
    "utcnow_iso",
]
