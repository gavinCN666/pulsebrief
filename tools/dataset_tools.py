"""Tool 1 — inspect_dataset."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from tools.base import BaseTool, StrictModel, ToolContext
from tools.common import get_dataset_or_raise


class InspectDatasetInput(StrictModel):
    dataset_id: str = Field(min_length=3, description="Dataset id returned by the upload step.")


class DateRange(StrictModel):
    start: str
    end: str


class DatasetIssue(StrictModel):
    code: str
    message: str
    field: str | None = None
    row: int | None = None
    severity: str = "error"


class InspectDatasetOutput(StrictModel):
    dataset_id: str
    valid: bool
    analysis_ready: bool
    status: str
    row_count: int
    valid_row_count: int
    date_range: DateRange | None = None
    available_metrics: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    duplicate_dates: list[str] = Field(default_factory=list)
    missing_dates: list[str] = Field(default_factory=list)
    missing_value_counts: dict[str, int] = Field(default_factory=dict)
    issues: list[DatasetIssue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    summary: str = ""
    recommended_action: str = "analyze"


class InspectDatasetTool(BaseTool):
    name = "inspect_dataset"
    description = (
        "Check an uploaded health dataset before analysis: schema completeness, valid date range, "
        "missing values, duplicates, invalid rows and whether enough days exist for trend analysis. "
        "Call this first; never assume a dataset is usable."
    )
    category = "read"
    input_model = InspectDatasetInput
    output_model = InspectDatasetOutput

    def execute(self, ctx: ToolContext, payload: InspectDatasetInput) -> InspectDatasetOutput:
        dataset = get_dataset_or_raise(ctx.repos, payload.dataset_id)
        validation: dict[str, Any] = dataset.validation or {}
        db_rows = ctx.repos.datasets.rows(dataset.dataset_id)

        issues: list[dict[str, Any]] = list(validation.get("issues", []))
        valid_row_count = int(validation.get("valid_row_count", len(db_rows)))
        if len(db_rows) != valid_row_count:
            issues.append(
                {
                    "code": "row_count_mismatch",
                    "message": (
                        f"Stored rows ({len(db_rows)}) differ from the validation record "
                        f"({valid_row_count}). Treat the dataset as unverified."
                    ),
                    "field": None,
                    "row": None,
                    "severity": "error",
                }
            )

        valid = bool(validation.get("valid", False)) and not any(
            i["severity"] == "error" and i["code"] == "row_count_mismatch" for i in issues
        )
        analysis_ready = bool(validation.get("analysis_ready", False)) and valid
        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]

        start = (validation.get("date_range") or {}).get("start")
        end = (validation.get("date_range") or {}).get("end")

        if not valid:
            recommended = "fix_data"
            summary = f"数据校验未通过：{len(errors)} 个错误，数据不能用于分析。"
        elif not analysis_ready:
            recommended = "ask_for_more_data"
            summary = (
                f"数据格式有效，但可用记录仅 {valid_row_count} 天，"
                f"少于分析所需的 {ctx.config.min_analysis_days} 天，无法给出可靠的趋势结论。"
            )
        else:
            recommended = "analyze"
            summary = (
                f"数据校验通过：{valid_row_count} 天（{start} ~ {end}），"
                f"可用指标 {len(validation.get('available_metrics', []))} 个。"
            )
        if warnings:
            summary += f" 另有 {len(warnings)} 条提示，分析时需要说明。"

        return InspectDatasetOutput(
            dataset_id=dataset.dataset_id,
            valid=valid,
            analysis_ready=analysis_ready,
            status=dataset.status.value,
            row_count=int(validation.get("row_count", len(db_rows))),
            valid_row_count=valid_row_count,
            date_range=DateRange(start=start, end=end) if start and end else None,
            available_metrics=list(validation.get("available_metrics", [])),
            missing_fields=list(validation.get("missing_fields", [])),
            duplicate_dates=list(validation.get("duplicate_dates", [])),
            missing_dates=list(validation.get("missing_dates", [])),
            missing_value_counts=dict(validation.get("missing_values", {})),
            issues=[DatasetIssue(**issue) for issue in issues],
            error_count=len(errors),
            warning_count=len(warnings),
            summary=summary,
            recommended_action=recommended,
        )
