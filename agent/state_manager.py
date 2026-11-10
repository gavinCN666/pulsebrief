"""Persist and restore run state."""

from __future__ import annotations

from storage.models import AgentRun, RunStatus, utc_now_iso
from storage.repositories import Repositories

from agent.state import RunState


class StateManager:
    def __init__(self, repos: Repositories):
        self.repos = repos

    def create(self, state: RunState) -> RunState:
        run = AgentRun(
            run_id=state.run_id,
            task_id=None,
            user_id=state.user_id,
            dataset_id=state.dataset_id,
            user_request=state.user_request,
            status=RunStatus(state.status),
            created_at=state.started_at,
            updated_at=state.started_at,
        )
        self.repos.runs.create(run)
        self.save(state)
        return state

    def load(self, run_id: str) -> RunState:
        run = self.repos.runs.get(run_id)
        if run is None:
            raise ValueError(f"Agent run {run_id!r} does not exist.")
        data = self.repos.runs.get_state_json(run_id)
        if not data:
            data = {
                "run_id": run.run_id,
                "user_id": run.user_id,
                "dataset_id": run.dataset_id,
                "user_request": run.user_request,
                "status": run.status.value,
                "started_at": run.created_at,
            }
        state = RunState.from_dict(data)
        state.status = run.status.value
        return state

    def save(self, state: RunState) -> None:
        self.repos.runs.update(
            state.run_id,
            status=state.status,
            state=state.to_dict(),
            error=state.error,
            completed_at=utc_now_iso() if RunStatus(state.status).is_terminal else None,
            tokens_prompt=state.tokens.get("prompt", 0),
            tokens_completion=state.tokens.get("completion", 0),
        )

    def set_status(self, state: RunState, status: RunStatus, *, error: str | None = None) -> RunState:
        state.status = status.value
        if error is not None:
            state.error = error
        self.save(state)
        return state
