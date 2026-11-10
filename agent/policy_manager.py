"""Policy manager: the program-side gate between the model and tool execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import HarnessConfig
from storage.models import utc_now
from storage.repositories import Repositories
from tools.action_tools import plan_payload
from tools.base import BaseTool
from tools.schemas import PlanItem

from agent.approvals import create_approval
from agent.state import RunState


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    code: str = "ALLOWED"
    message: str = ""
    approval_id: str | None = None


class PolicyManager:
    def __init__(self, config: HarnessConfig, repos: Repositories):
        self.config = config
        self.repos = repos

    def check(self, state: RunState, tool: BaseTool, payload: Any) -> PolicyDecision:
        if state.tool_call_count >= self.config.max_tool_calls_per_run:
            return PolicyDecision(
                allowed=False,
                code="TOOL_CALL_LIMIT",
                message=f"Tool call limit ({self.config.max_tool_calls_per_run}) reached.",
            )

        if state.started_at:
            try:
                elapsed = (utc_now() - datetime.fromisoformat(state.started_at)).total_seconds()
            except ValueError:
                elapsed = 0.0
            if elapsed > self.config.max_run_seconds:
                return PolicyDecision(
                    allowed=False,
                    code="RUN_TIME_LIMIT",
                    message="Run time limit exceeded; stopping for safety.",
                )

        signature = self._signature(tool.name, payload)
        if state.call_history.count(signature) >= self.config.max_identical_tool_calls:
            return PolicyDecision(
                allowed=False,
                code="DUPLICATE_CALL",
                message=(
                    f"Identical {tool.name} call repeated more than "
                    f"{self.config.max_identical_tool_calls} times; blocked to avoid loops."
                ),
            )
        state.call_history.append(signature)

        if tool.category != "write":
            return PolicyDecision(allowed=True)

        # Write tools always require a fresh, payload-bound approval. The presence
        # of an approval_id in model arguments is not enough: policy issues it.
        plan = getattr(payload, "plan", [])
        if not isinstance(plan, list) or not all(isinstance(item, PlanItem) for item in plan):
            return PolicyDecision(
                allowed=False,
                code="INVALID_WRITE_PAYLOAD",
                message="Write payload is missing a valid plan.",
            )

        # A write must be the exact plan that passed deterministic validation.
        validation = state.validation or {}
        normalized = validation.get("normalized_plan") or []
        if not validation.get("valid") or not normalized:
            return PolicyDecision(
                allowed=False,
                code="PLAN_NOT_VALIDATED",
                message="The plan has not passed validate_action_plan; write blocked.",
            )
        expected = [PlanItem.model_validate(item) for item in normalized]
        if [item.model_dump(mode="json") for item in plan] != [
            item.model_dump(mode="json") for item in expected
        ]:
            return PolicyDecision(
                allowed=False,
                code="PLAN_MISMATCH",
                message="The plan to write differs from the validated normalized_plan.",
            )

        approval_payload = plan_payload(plan, state.dataset_id)
        approval = create_approval(
            self.repos,
            run_id=state.run_id,
            tool_name=tool.name,
            payload=approval_payload,
            ttl_minutes=self.config.approval_ttl_minutes,
        )
        return PolicyDecision(
            allowed=False,
            requires_approval=True,
            code="APPROVAL_REQUIRED",
            message="This write operation requires explicit user approval.",
            approval_id=approval.approval_id,
        )

    def _signature(self, tool_name: str, payload: Any) -> str:
        dumped = (
            payload.model_dump(mode="json") if hasattr(payload, "model_dump") else str(payload)
        )
        canonical = json.dumps(dumped, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(f"{tool_name}:{canonical}".encode("utf-8")).hexdigest()[:16]
