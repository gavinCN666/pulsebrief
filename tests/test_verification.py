"""Verification tests: check the database, never the model's claim."""

from __future__ import annotations

from agent.verification import verify_task_creation
from tests.helpers import build_env


def _complete_run(env):
    run_id = env.harness.start_run("分析并创建任务。", env.user_id, env.dataset.dataset_id)
    outcome = env.harness.run(run_id)
    return env.harness.approve(
        run_id, outcome.pending_approval["approval_id"], approved=True
    )


def test_verification_confirms_database_state(root):
    env = build_env(root, "clean_30d")
    outcome = _complete_run(env)
    assert outcome.verification["ok"] is True
    assert outcome.verification["actual_count"] == 4
    assert all(check["ok"] for check in outcome.verification["checks"])


def test_verification_detects_a_missing_row(root):
    env = build_env(root, "clean_30d")
    outcome = _complete_run(env)
    assert outcome.state is not None
    batch_key = outcome.state.write_result["batch_key"]
    approval_id = outcome.state.approval_id
    task_ids = outcome.state.write_result["task_ids"]

    with env.repos.db.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_ids[0],))
        conn.commit()

    result = verify_task_creation(
        env.repos,
        run_id=outcome.run_id,
        expected_task_ids=task_ids,
        approval_id=approval_id,
        batch_key=batch_key,
        duplicate=False,
    )
    assert result.ok is False
    assert result.actual_count == 3
    failed = {check.code for check in result.checks if not check.ok}
    assert "count_matches" in failed


def test_verification_failure_marks_run_failed(root):
    env = build_env(root, "clean_30d", faults={"verification": 1})
    run_id = env.harness.start_run("分析并创建任务。", env.user_id, env.dataset.dataset_id)
    outcome = env.harness.run(run_id)
    outcome = env.harness.approve(
        run_id, outcome.pending_approval["approval_id"], approved=True
    )

    assert outcome.status == "FAILED"
    assert outcome.verification is not None and outcome.verification["ok"] is False
    assert "VERIFICATION_FAILED" in (outcome.error or "")
    assert "校验失败" in (outcome.final_answer or "")
    assert len(env.repos.tasks.list_all(env.user_id)) == 4
