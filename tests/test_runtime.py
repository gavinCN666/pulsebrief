"""Runtime tests: the loop, pause/resume, context size and limits."""

from __future__ import annotations

from config import ProviderSettings, Settings
from storage.models import ApprovalStatus

from agent.harness import AgentHarness
from agent.provider import BaseProvider, LLMResponse, ToolCall
from tests.helpers import build_env


class StuckProvider(BaseProvider):
    """Always asks for the same read tool, to exercise the step limit."""

    name = "stuck"

    def complete(self, request):  # noqa: ANN001
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="stuck",
                    name="inspect_dataset",
                    arguments={"dataset_id": request.state["dataset_id"]},
                )
            ],
        )


def test_end_to_end_approval_flow(root):
    env = build_env(root, "clean_30d")
    run_id = env.harness.start_run(
        "帮我分析最近一个月的健康状态，并制定下一周计划。",
        env.user_id,
        env.dataset.dataset_id,
    )
    outcome = env.harness.run(run_id)
    assert outcome.status == "WAITING_FOR_APPROVAL"
    assert outcome.pending_approval is not None
    approval_id = outcome.pending_approval["approval_id"]

    outcome = env.harness.approve(run_id, approval_id, approved=True)
    assert outcome.status == "COMPLETED"
    assert outcome.verification is not None and outcome.verification["ok"] is True
    assert len(env.repos.tasks.list_all(env.user_id)) == 4
    assert "任务 ID" in (outcome.final_answer or "")
    assert outcome.state is not None
    assert outcome.state.used_tools.count("detect_trends") == 4

    traces = env.repos.traces.list_for_run(run_id)
    action_types = {trace.action_type.value for trace in traces}
    assert {"LLM", "TOOL", "APPROVAL", "VERIFICATION"}.issubset(action_types)
    approval = env.repos.approvals.get(approval_id)
    assert approval.status == ApprovalStatus.CONSUMED


def test_rejection_creates_nothing(root):
    env = build_env(root, "clean_30d")
    run_id = env.harness.start_run("分析并创建任务。", env.user_id, env.dataset.dataset_id)
    outcome = env.harness.run(run_id)
    approval_id = outcome.pending_approval["approval_id"]
    outcome = env.harness.approve(run_id, approval_id, approved=False)
    assert outcome.status == "COMPLETED"
    assert env.repos.tasks.list_all(env.user_id) == []
    assert "没有创建" in (outcome.final_answer or "")
    approval = env.repos.approvals.get(approval_id)
    assert approval.status == ApprovalStatus.REJECTED


def test_context_never_contains_raw_csv(root):
    env = build_env(root, "clean_30d")
    run_id = env.harness.start_run("分析并制定计划。", env.user_id, env.dataset.dataset_id)
    env.harness.run(run_id)
    state = env.harness.get_state(run_id)
    messages = env.harness.context_manager.build(state)
    joined = "\n".join(message["content"] for message in messages)
    assert "date,sleep_hours" not in joined
    assert "2026-10-06,6.8" not in joined
    assert "sleep_hours" in joined
    assert len(joined) < env.settings.harness.max_context_chars * 2


def test_max_steps_is_enforced(root):
    env = build_env(root, "clean_30d", config_overrides={"max_steps": 3}, provider=StuckProvider())
    run_id = env.harness.start_run("分析数据。", env.user_id, env.dataset.dataset_id)
    outcome = env.harness.run(run_id)
    assert outcome.status == "FAILED"
    assert "MAX_STEPS_REACHED" in (outcome.error or "")
    assert env.repos.tasks.list_all(env.user_id) == []


def test_paused_run_resumes_in_a_new_harness_instance(root):
    env = build_env(root, "clean_30d")
    run_id = env.harness.start_run("分析并创建任务。", env.user_id, env.dataset.dataset_id)
    first = env.harness.run(run_id)
    assert first.status == "WAITING_FOR_APPROVAL"

    fresh = AgentHarness(
        settings=Settings(
            harness=env.settings.harness,
            provider=ProviderSettings(provider="mock"),
        ),
        repos=env.repos,
    )
    state = fresh.get_state(run_id)
    assert state.pending_call is not None

    outcome = fresh.approve(
        run_id, first.pending_approval["approval_id"], approved=True
    )
    assert outcome.status == "COMPLETED"
    assert len(env.repos.tasks.list_all(env.user_id)) == 4


def test_cancel_pending_run(root):
    env = build_env(root, "clean_30d")
    run_id = env.harness.start_run("分析并创建任务。", env.user_id, env.dataset.dataset_id)
    outcome = env.harness.run(run_id)
    assert outcome.status == "WAITING_FOR_APPROVAL"

    cancelled = env.harness.cancel(run_id, reason="user changed their mind")
    assert cancelled.status == "CANCELLED"
    assert env.repos.tasks.list_all(env.user_id) == []
    approval = env.repos.approvals.get(outcome.pending_approval["approval_id"])
    assert approval.status == ApprovalStatus.REJECTED
