"""Permission and approval tests: the security core of the project."""

from __future__ import annotations

import pytest

from storage.models import ApprovalStatus
from tools.action_tools import CreateActionTasksTool, plan_payload
from tools.base import ApprovalRequiredError
from tools.schemas import PlanItem
from tools.validation_tools import ValidateActionPlanTool

from agent.approvals import create_approval, decide_approval
from agent.policy_manager import PolicyManager
from agent.state import RunState
from tests.helpers import BASE_PLAN, build_env, make_ctx, new_run


def test_write_blocked_without_approval(root):
    env = build_env(root, "clean_30d")
    run_id = env.harness.start_run("分析并创建任务。", env.user_id, env.dataset.dataset_id)
    outcome = env.harness.run(run_id)

    assert outcome.status == "WAITING_FOR_APPROVAL"
    assert env.repos.tasks.list_all(env.user_id) == []
    pending = env.repos.approvals.pending_for_run(run_id)
    assert pending is not None and pending.status == ApprovalStatus.PENDING
    state = env.harness.get_state(run_id)
    assert state.pending_call is not None
    assert state.pending_call["tool_name"] == "create_action_tasks"


def test_expired_approval_cannot_write(root):
    env = build_env(root, "clean_30d")
    ctx = make_ctx(env)
    validated = ValidateActionPlanTool().run(ctx, {"plan": BASE_PLAN})
    plan = [PlanItem(**item) for item in validated["normalized_plan"]]
    approval = create_approval(
        env.repos,
        run_id=ctx.run_id,
        tool_name="create_action_tasks",
        payload=plan_payload(plan, env.dataset.dataset_id),
        ttl_minutes=-1,
    )

    with pytest.raises(ValueError, match="expired"):
        decide_approval(env.repos, approval.approval_id, approved=True)
    assert env.repos.approvals.get(approval.approval_id).status == ApprovalStatus.EXPIRED

    with pytest.raises(ApprovalRequiredError):
        CreateActionTasksTool().run(
            ctx,
            {"plan": validated["normalized_plan"], "approval_id": approval.approval_id},
        )
    assert env.repos.tasks.list_all(env.user_id) == []


def test_approval_is_bound_to_run(root):
    env = build_env(root, "clean_30d")
    ctx_one = make_ctx(env)
    validated = ValidateActionPlanTool().run(ctx_one, {"plan": BASE_PLAN})
    plan = [PlanItem(**item) for item in validated["normalized_plan"]]
    approval = create_approval(
        env.repos,
        run_id=ctx_one.run_id,
        tool_name="create_action_tasks",
        payload=plan_payload(plan, env.dataset.dataset_id),
        ttl_minutes=30,
    )
    decide_approval(env.repos, approval.approval_id, approved=True)

    other_run = new_run(env.repos, env.user_id, env.dataset.dataset_id)
    other_ctx = make_ctx(env, run_id=other_run)
    with pytest.raises(ApprovalRequiredError, match="different agent run"):
        CreateActionTasksTool().run(
            other_ctx,
            {"plan": validated["normalized_plan"], "approval_id": approval.approval_id},
        )


def test_policy_blocks_repeated_identical_calls(root):
    env = build_env(root, "clean_30d")
    policy = PolicyManager(env.settings.harness, env.repos)
    state = RunState(
        run_id=new_run(env.repos, env.user_id, env.dataset.dataset_id),
        user_id=env.user_id,
        user_request="test",
        dataset_id=env.dataset.dataset_id,
    )
    from tools.dataset_tools import InspectDatasetTool

    tool = InspectDatasetTool()
    payload = tool.input_model.model_validate({"dataset_id": env.dataset.dataset_id})

    first = policy.check(state, tool, payload)
    second = policy.check(state, tool, payload)
    third = policy.check(state, tool, payload)
    assert first.allowed and second.allowed
    assert third.allowed is False
    assert third.code == "DUPLICATE_CALL"
