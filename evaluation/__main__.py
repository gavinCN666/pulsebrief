"""CLI entry point: ``python -m evaluation``."""

from __future__ import annotations

from evaluation.evaluator import run_suite


def main() -> None:
    record = run_suite(save_report=True)
    metrics = record.metrics
    print(f"cases: {record.passed}/{record.total}")
    print(f"task_completion_rate: {metrics.get('task_completion_rate')}")
    print(f"tool_selection_accuracy: {metrics.get('tool_selection_accuracy')}")
    print(f"metric_calculation_accuracy: {metrics.get('metric_calculation_accuracy')}")
    print(f"missing_data_detection: {metrics.get('missing_data_detection')}")
    print(f"unauthorized_write_count: {metrics.get('unauthorized_write_count')}")
    print(f"state_verification_accuracy: {metrics.get('state_verification_accuracy')}")
    print(f"failure_recovery_rate: {metrics.get('failure_recovery_rate')}")
    print(f"average_tool_calls: {metrics.get('average_tool_calls')}")
    print(f"average_task_duration_ms: {metrics.get('average_task_duration_ms')}")
    print("report: docs/evaluation_report.md")
    failed = [item["id"] for item in record.results if not item["passed"]]
    if failed:
        print("failed cases: " + ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
