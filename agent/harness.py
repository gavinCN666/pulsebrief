"""AgentHarness facade — the public entry point for UI, tests and evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import Settings
from storage.models import (
    ActionType,
    AgentRun,
    ApprovalStatus,
    RunStatus,
    ToolTrace,
    TraceStatus,
    new_id,
    utc_now_iso,
)
from storage.repositories import Repositories

from agent.approvals import decide_approval
from agent.context_manager import ContextManager
from agent.policy_manager import PolicyManager
from agent.provider import BaseProvider, build_provider
from agent.runtime import AgentRuntime, ToolExecutor
from agent.state import RunState
from agent.state_manager import StateManager
from agent.tool_registry import ToolRegistry


@dataclass
class RunOutcome:
    run_id: str
    status: str
    final_answer: str | None
    error: str | None
    pending_approval: dict[str, Any] | None
    verification: dict[str, Any] | None
    steps: int
    tool_calls: int
    state: RunState | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "final_answer": self.final_answer,
            "error": self.error,
            "pending_approval": self.pending_approval,
            "verification": self.verification,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "used_tools": self.state.used_tools if self.state else [],
            "trends": self.state.trends if self.state else {},
            "validation": self.state.validation if self.state else None,
            "write_result": self.state.write_result if self.state else None,
            "dataset": self.state.dataset if self.state else None,
        }


class AgentHarness:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        repos: Repositories | None = None,
        provider: BaseProvider | None = None,
        registry: ToolRegistry | None = None,
        fault_injection: dict[str, int] | None = None,
    ):
        self.settings = settings or Settings.load()
        self.config = self.settings.harness
        self.repos = repos or Repositories.open(self.config.db_path)
        self.registry = registry or ToolRegistry()
        self.provider = provider or build_provider(self.settings.provider)
        self.state_manager = StateManager(self.repos)
        self.context_manager = ContextManager(self.config)
        self.policy_manager = PolicyManager(self.config, self.repos)
        self.executor = ToolExecutor(self.config, fault_injection)
        self.runtime = AgentRuntime(
            config=self.config,
            repos=self.repos,
            registry=self.registry,
            provider=self.provider,
            context_manager=self.context_manager,
            policy_manager=self.policy_manager,
            state_manager=self.state_manager,
            executor=self.executor,
        )

    # ------------------------------------------------------------------- setup

    def provider_info(self) -> dict[str, str]:
        return self.provider.info()

    def start_run(self, user_request: str, user_id: str, dataset_id: str | None = None) -> str:
        request = (user_request or "").strip()
        if not request:
            raise ValueError("The user request must not be empty.")
        if self.repos.users.get(user_id) is None:
            raise ValueError(f"User {user_id!r} does not exist. Create the profile first.")
        if dataset_id:
            dataset = self.repos.datasets.get(dataset_id)
            if dataset is None:
                raise ValueError(f"Dataset {dataset_id!r} does not exist.")
            if dataset.user_id != user_id:
                raise ValueError("The dataset belongs to a different user.")

        self._assert_no_active_run(user_id)
        run_id = new_id("run")
        state = RunState(
            run_id=run_id,
            user_id=user_id,
            dataset_id=dataset_id,
            user_request=request,
            status=RunStatus.CREATED.value,
        )
        self.state_manager.create(state)
        return run_id

    def _assert_no_active_run(self, user_id: str) -> None:
        if self.config.max_concurrent_runs <= 1:
            active_statuses = {
                RunStatus.CREATED.value,
                RunStatus.RUNNING.value,
                RunStatus.WAITING_FOR_APPROVAL.value,
                RunStatus.APPROVED.value,
            }
            for run in self.repos.runs.list_all(user_id, limit=20):
                if run.status in active_statuses:
                    raise ValueError(
                        f"Run {run.run_id} is still {run.status}. "
                        "Finish, cancel or approve it before starting a new run."
                    )

    # ------------------------------------------------------------------ driving

    def run(self, run_id: str) -> RunOutcome:
        state = self.state_manager.load(run_id)
        if state.status in {
            RunStatus.WAITING_FOR_APPROVAL.value,
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        }:
            return self._outcome(state)
        state = self.runtime.run(state)
        return self._outcome(state)

    def approve(
        self,
        run_id: str,
        approval_id: str,
        *,
        approved: bool = True,
        reason: str | None = None,
    ) -> RunOutcome:
        state = self.state_manager.load(run_id)
        if state.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            raise ValueError(f"Run is already {state.status}.")
        if not state.pending_call or state.approval_id != approval_id:
            raise ValueError("This approval does not match the run's pending operation.")

        approval = decide_approval(
            self.repos,
            approval_id,
            approved=approved,
            reason=reason,
            decided_by="user",
        )

        self.repos.traces.add(
            ToolTrace(
                trace_id=new_id("trace"),
                run_id=state.run_id,
                step_index=state.step_index,
                action_type=ActionType.APPROVAL,
                tool_name=approval.tool_name,
                tool_input=state.pending_call.get("arguments"),
                output={
                    "approval_id": approval_id,
                    "status": approval.status.value,
                    "reason": reason,
                },
                output_summary=(
                    "user approved the write" if approved else "user rejected the write"
                ),
                status=TraceStatus.SUCCESS,
                duration_ms=0.0,
            )
        )

        if not approved:
            state.pending_call = None
            state.status = RunStatus.COMPLETED.value
            state.final_answer = (
                "你已拒绝这次写入，因此没有创建任何行动任务。\n\n"
                "本次趋势分析结果仍然保留；如果之后想继续，可以调整计划后重新发起。"
            )
            self.state_manager.save(state)
            return self._outcome(state)

        state.status = RunStatus.RUNNING.value
        state.started_at = utc_now_iso()
        self.state_manager.save(state)
        state = self.runtime.resume_write(state)
        if state.status in {
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
            RunStatus.WAITING_FOR_APPROVAL.value,
        }:
            return self._outcome(state)
        state = self.runtime.run(state)
        return self._outcome(state)

    def cancel(self, run_id: str, reason: str | None = None) -> RunOutcome:
        state = self.state_manager.load(run_id)
        if state.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            return self._outcome(state)

        if state.approval_id:
            approval = self.repos.approvals.get(state.approval_id)
            if approval is not None and approval.status == ApprovalStatus.PENDING:
                try:
                    decide_approval(
                        self.repos,
                        state.approval_id,
                        approved=False,
                        reason=reason or "Run cancelled by user",
                    )
                except ValueError:
                    pass
        state.cancelled = True
        state.pending_call = None
        state.status = RunStatus.CANCELLED.value
        state.final_answer = "任务已取消，未创建新的行动任务。"
        self.state_manager.save(state)
        return self._outcome(state)

    # ------------------------------------------------------------------- reads

    def get_state(self, run_id: str) -> RunState:
        return self.state_manager.load(run_id)

    def outcome(self, run_id: str) -> RunOutcome:
        return self._outcome(self.state_manager.load(run_id))

    def get_run(self, run_id: str) -> AgentRun | None:
        return self.repos.runs.get(run_id)

    def list_runs(self, user_id: str | None = None, limit: int = 30) -> list[AgentRun]:
        return self.repos.runs.list_all(user_id, limit=limit)

    def _outcome(self, state: RunState) -> RunOutcome:
        pending = self.repos.approvals.pending_for_run(state.run_id)
        return RunOutcome(
            run_id=state.run_id,
            status=state.status,
            final_answer=state.final_answer,
            error=state.error,
            pending_approval=pending.model_dump(mode="json") if pending else None,
            verification=state.verification,
            steps=state.step_index,
            tool_calls=state.tool_call_count,
            state=state,
        )
