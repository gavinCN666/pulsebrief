"""Tool 6 — create_action_tasks (the only write tool).

The harness pauses for human approval before this tool can run. Approval is
enforced here as well, at the data layer: an APPROVED record bound to this run
and this exact payload is required. A prompt-level "please ask the user" is not
sufficient.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from pydantic import Field

from storage.models import ActionTask, ApprovalStatus, TaskStatus, new_id, utc_now, utc_now_iso
from tools.base import (
    ApprovalRequiredError,
    BaseTool,
    StrictModel,
    ToolContext,
    ToolInputError,
    ToolStateError,
)
from tools.common import parse_iso_date
from tools.schemas import PlanItem, TaskOut


def plan_payload(plan: list[PlanItem], dataset_id: str | None) -> dict:
    return {
        "dataset_id": dataset_id,
        "plan": [item.model_dump(mode="json") for item in plan],
    }


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def batch_key(payload: dict) -> str:
    return "batch_" + payload_hash(payload)[:24]


class CreateActionTasksInput(StrictModel):
    plan: list[PlanItem] = Field(min_length=1, max_length=10)
    approval_id: str | None = Field(
        default=None,
        description="Approval id issued by the harness after the user approves this exact plan.",
    )


class CreateActionTasksOutput(StrictModel):
    created: bool
    duplicate: bool
    task_count: int
    task_ids: list[str] = Field(default_factory=list)
    tasks: list[TaskOut] = Field(default_factory=list)
    batch_key: str
    approval_id: str | None = None
    message: str = ""


def _task_out(task: ActionTask) -> TaskOut:
    return TaskOut(
        task_id=task.task_id,
        title=task.title,
        description=task.description,
        frequency=task.frequency,
        duration_days=task.duration_days,
        scheduled_date=task.scheduled_date,
        status=task.status.value,
        target_metric=task.target_metric,
    )


class CreateActionTasksTool(BaseTool):
    name = "create_action_tasks"
    description = (
        "Persist an approved action plan as real tasks. Requires a valid one-time approval "
        "issued for this exact plan; the harness will pause for the user before this runs. "
        "Idempotent: submitting the same plan twice returns the existing tasks."
    )
    category = "write"
    input_model = CreateActionTasksInput
    output_model = CreateActionTasksOutput

    def execute(self, ctx: ToolContext, payload: CreateActionTasksInput) -> CreateActionTasksOutput:
        if not ctx.run_id:
            raise ToolInputError("create_action_tasks must run inside an agent run.")

        for index, item in enumerate(payload.plan):
            if not item.scheduled_date:
                raise ToolInputError(
                    f"Plan item {index + 1} has no scheduled_date. "
                    "Use the normalized_plan returned by validate_action_plan."
                )
            parse_iso_date(item.scheduled_date, "scheduled_date")

        expected_payload = plan_payload(payload.plan, ctx.dataset_id)
        expected_hash = payload_hash(expected_payload)
        batch = batch_key(expected_payload)

        approval_id = payload.approval_id or ctx.approval_id
        if not approval_id:
            raise ApprovalRequiredError(
                "No approval_id was provided. This write is blocked until the user approves the plan."
            )
        approval = ctx.repos.approvals.get(approval_id)
        if approval is None:
            raise ApprovalRequiredError(f"Approval {approval_id!r} does not exist.")
        if approval.run_id != ctx.run_id:
            raise ApprovalRequiredError("The approval belongs to a different agent run.")
        if approval.tool_name != self.name:
            raise ApprovalRequiredError(
                f"The approval is for {approval.tool_name!r}, not {self.name!r}."
            )
        if approval.payload_hash != expected_hash:
            raise ApprovalRequiredError(
                "The approved payload does not match the current plan. "
                "Approval is bound to the exact plan the user reviewed."
            )

        existing = ctx.repos.tasks.by_batch(batch)
        if existing:
            return CreateActionTasksOutput(
                created=False,
                duplicate=True,
                task_count=len(existing),
                task_ids=[t.task_id for t in existing],
                tasks=[_task_out(t) for t in existing],
                batch_key=batch,
                approval_id=approval_id,
                message=f"相同计划已创建过 {len(existing)} 个任务，本次未重复写入（幂等返回）。",
            )

        if approval.status == ApprovalStatus.CONSUMED:
            raise ApprovalRequiredError("This approval has already been consumed by another write.")
        if approval.status != ApprovalStatus.APPROVED:
            raise ApprovalRequiredError(
                f"Approval status is {approval.status.value}; only APPROVED approvals can write."
            )
        expires_at = datetime.fromisoformat(approval.expires_at)
        if expires_at <= utc_now():
            ctx.repos.approvals.set_status(
                approval.approval_id, ApprovalStatus.EXPIRED, reason="Approval TTL elapsed"
            )
            raise ApprovalRequiredError("The approval has expired. Ask the user to approve again.")

        now = utc_now_iso()
        tasks = [
            ActionTask(
                task_id=new_id("task"),
                user_id=ctx.user_id,
                run_id=ctx.run_id,
                dataset_id=ctx.dataset_id,
                title=item.title.strip(),
                description=item.description.strip(),
                frequency=item.frequency,
                duration_days=item.duration_days,
                scheduled_date=item.scheduled_date or "",
                status=TaskStatus.PENDING,
                idempotency_key=batch,
                task_index=index,
                target_metric=item.target_metric,
                created_at=now,
                updated_at=now,
            )
            for index, item in enumerate(payload.plan)
        ]

        try:
            ctx.repos.tasks.insert_batch(tasks)
        except sqlite3.IntegrityError:
            existing = ctx.repos.tasks.by_batch(batch)
            if existing:
                return CreateActionTasksOutput(
                    created=False,
                    duplicate=True,
                    task_count=len(existing),
                    task_ids=[t.task_id for t in existing],
                    tasks=[_task_out(t) for t in existing],
                    batch_key=batch,
                    approval_id=approval_id,
                    message="并发写入检测到相同计划，已返回已有任务（幂等）。",
                )
            raise ToolStateError("Task insert failed due to a database constraint.")

        ctx.repos.approvals.consume(approval.approval_id)
        return CreateActionTasksOutput(
            created=True,
            duplicate=False,
            task_count=len(tasks),
            task_ids=[t.task_id for t in tasks],
            tasks=[_task_out(t) for t in tasks],
            batch_key=batch,
            approval_id=approval_id,
            message=f"已创建 {len(tasks)} 个行动计划任务，状态为 PENDING。",
        )
