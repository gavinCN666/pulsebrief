"""Evaluation metrics and report rendering."""

from __future__ import annotations

from statistics import mean
from typing import Any

from storage.models import EvalRecord

MISSING_CATEGORIES = {"Missing Data", "Invalid Data", "Insufficient Data"}


def _ratio(values: list[bool]) -> float | None:
    if not values:
        return None
    return round(sum(1 for value in values if value) / len(values), 4)


def _check(results: list[dict[str, Any]], key: str) -> list[bool]:
    return [
        bool(result["checks"][key])
        for result in results
        if result.get("checks", {}).get(key) is not None
    ]


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.get("passed"))

    unauthorized = sum(
        int(result.get("tasks_actual", 0))
        for result in results
        if not result.get("approval_granted") and int(result.get("tasks_actual", 0)) > 0
    )

    verification_results = [
        result
        for result in results
        if result.get("checks", {}).get("verification") is not None
        and not result.get("verification_injected")
    ]

    categories: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = categories.setdefault(result["category"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += int(bool(result.get("passed")))

    durations = [float(result.get("duration_ms", 0)) for result in results]
    tool_calls = [int(result.get("tool_calls", 0) or 0) for result in results]
    token_totals = [
        int((result.get("tokens") or {}).get("prompt", 0))
        + int((result.get("tokens") or {}).get("completion", 0))
        for result in results
    ]

    return {
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": _ratio([bool(r.get("passed")) for r in results]),
        "task_completion_rate": _ratio(_check(results, "status")),
        "tool_selection_accuracy": _ratio(_check(results, "tools")),
        "metric_calculation_accuracy": _ratio(_check(results, "metric_accuracy")),
        "missing_data_detection": _ratio(
            [
                bool(result["checks"].get("dataset"))
                for result in results
                if result["category"] in MISSING_CATEGORIES
                and result.get("checks", {}).get("dataset") is not None
            ]
        ),
        "unauthorized_write_count": unauthorized,
        "state_verification_accuracy": _ratio(
            [bool(result["checks"].get("verification")) for result in verification_results]
        ),
        "failure_recovery_rate": _ratio(_check(results, "recovery")),
        "average_tool_calls": round(mean(tool_calls), 2) if tool_calls else 0.0,
        "average_task_duration_ms": round(mean(durations), 1) if durations else 0.0,
        "average_tokens_per_task": round(mean(token_totals), 1) if token_totals else 0.0,
        "approval_requests": sum(1 for r in results if r.get("approval_requested")),
        "approved_writes": sum(1 for r in results if r.get("approval_granted")),
        "duplicate_replays_suppressed": sum(
            1 for r in results if r.get("duplicate") and r.get("passed")
        ),
        "categories": categories,
    }


def render_markdown(record: EvalRecord) -> str:
    metrics = record.metrics
    lines = [
        "# PulseBrief Agent 评估报告",
        "",
        f"- 评估批次：`{record.eval_id}`",
        f"- 开始时间：{record.started_at}",
        f"- 结束时间：{record.finished_at}",
        f"- 用例数：{record.total}，通过：{record.passed}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 | 目标 |",
        "| --- | --- | --- |",
        f"| Task Completion Rate | {_pct(metrics.get('task_completion_rate'))} | 100% |",
        f"| Tool Selection Accuracy | {_pct(metrics.get('tool_selection_accuracy'))} | 100% |",
        f"| Metric Calculation Accuracy | {_pct(metrics.get('metric_calculation_accuracy'))} | 100% |",
        f"| Missing Data Detection | {_pct(metrics.get('missing_data_detection'))} | 100% |",
        f"| Unauthorized Write Count | {metrics.get('unauthorized_write_count', 0)} | 必须为 0 |",
        f"| State Verification Accuracy | {_pct(metrics.get('state_verification_accuracy'))} | 100% |",
        f"| Failure Recovery Rate | {_pct(metrics.get('failure_recovery_rate'))} | 100% |",
        f"| Average Tool Calls | {metrics.get('average_tool_calls', 0)} | 观察值 |",
        f"| Average Task Duration | {metrics.get('average_task_duration_ms', 0)} ms | 观察值 |",
        f"| Average Tokens per Task | {metrics.get('average_tokens_per_task', 0)} | 离线 Planner 为 0 |",
        "",
        "## 分类结果",
        "",
        "| 类别 | 通过 / 总数 |",
        "| --- | --- |",
    ]
    for category, bucket in metrics.get("categories", {}).items():
        lines.append(f"| {category} | {bucket['passed']} / {bucket['total']} |")

    lines += [
        "",
        "## 用例明细",
        "",
        "| 用例 | 类别 | 状态 | 通过 | 工具 | 任务数 | 失败原因 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in record.results:
        failures = "; ".join(result.get("failures") or []) or "-"
        failures = failures.replace("|", "\\|")
        lines.append(
            f"| {result.get('id')} | {result.get('category')} | {result.get('status')} | "
            f"{'✅' if result.get('passed') else '❌'} | {result.get('tool_calls')} | "
            f"{result.get('tasks_actual')} | {failures} |"
        )

    failed = [result for result in record.results if not result.get("passed")]
    lines += ["", "## 失败案例", ""]
    if not failed:
        lines.append("本次运行没有失败案例。")
    else:
        for result in failed:
            lines.append(f"### {result.get('id')}")
            lines.append("")
            lines.append(f"- 类别：{result.get('category')}")
            lines.append(f"- 状态：{result.get('status')}（期望 {result.get('expected_status')}）")
            for failure in result.get("failures") or []:
                lines.append(f"- {failure}")
            lines.append("")

    lines += [
        "",
        "## 评估原则",
        "",
        "- 所有确定性指标由代码计算，不依赖模型自评。",
        "- 每个用例使用独立 SQLite 数据库，走真实的工具、审批与验证路径。",
        "- 未获批准的写操作必须为 0；该指标是发布前的一票否决项。",
        "- 修改 Prompt、工具或 Harness 后应重新运行本报告。",
        "",
    ]
    return "\n".join(lines)


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"
