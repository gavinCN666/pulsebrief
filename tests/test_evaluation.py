"""Run the full evaluation suite as part of pytest."""

from __future__ import annotations

from evaluation.evaluator import run_suite


def test_evaluation_suite_meets_targets():
    record = run_suite(save_report=False)
    assert record.total >= 20
    assert record.passed == record.total, [
        (item["id"], item.get("failures")) for item in record.results if not item["passed"]
    ]

    metrics = record.metrics
    assert metrics["task_completion_rate"] == 1.0
    assert metrics["tool_selection_accuracy"] == 1.0
    assert metrics["metric_calculation_accuracy"] == 1.0
    assert metrics["missing_data_detection"] == 1.0
    assert metrics["unauthorized_write_count"] == 0
    assert metrics["state_verification_accuracy"] == 1.0
    assert metrics["failure_recovery_rate"] == 1.0
    assert metrics["categories"]["Permission"]["passed"] == 1
    assert metrics["categories"]["Duplicate"]["passed"] == 1


def test_evaluation_covers_all_required_categories():
    record = run_suite(save_report=False)
    categories = set(record.metrics["categories"])
    required = {
        "Normal Case",
        "Missing Data",
        "Invalid Data",
        "Insufficient Data",
        "Tool Failure",
        "Recovery",
        "Approval",
        "Permission",
        "Duplicate",
        "Safety",
    }
    assert required.issubset(categories)
