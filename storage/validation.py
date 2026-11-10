"""Deterministic CSV validation.

Design rule from the PRD: never silently turn unconfirmable data into a normal
value. Every malformed, missing or duplicate value becomes a structured issue
that the agent (and the user) can see.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

REQUIRED_COLUMNS = [
    "date",
    "sleep_hours",
    "steps",
    "active_minutes",
    "exercise_minutes",
    "resting_hr",
]
METRIC_COLUMNS = REQUIRED_COLUMNS[1:]

RANGES = {
    "sleep_hours": (0.0, 24.0),
    "steps": (0, 200_000),
    "active_minutes": (0, 1440),
    "exercise_minutes": (0, 1440),
    "resting_hr": (30, 220),
}
PLAUSIBLE = {
    "sleep_hours": (3.0, 14.0),
    "steps": (500, 50_000),
    "active_minutes": (0, 600),
    "exercise_minutes": (0, 300),
    "resting_hr": (40, 120),
}
INTEGER_METRICS = {"steps", "active_minutes", "exercise_minutes", "resting_hr"}


def decode_csv_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


@dataclass
class Issue:
    code: str
    message: str
    field: str | None = None
    row: int | None = None
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "row": self.row,
            "severity": self.severity,
        }


@dataclass
class ValidationReport:
    row_count: int = 0
    valid_row_count: int = 0
    columns: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    available_metrics: list[str] = field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    clean_rows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    missing_values: dict[str, int] = field(default_factory=dict)
    duplicate_dates: list[str] = field(default_factory=list)
    missing_dates: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def analysis_ready(self) -> bool:
        if not self.valid or self.valid_row_count == 0:
            return False
        if any(i.code == "insufficient_data" for i in self.issues):
            return False
        return True

    @property
    def status(self) -> str:
        if not self.valid:
            return "INVALID"
        return "READY" if self.analysis_ready else "INSUFFICIENT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "analysis_ready": self.analysis_ready,
            "status": self.status,
            "row_count": self.row_count,
            "valid_row_count": self.valid_row_count,
            "columns": self.columns,
            "date_range": (
                {"start": self.start_date, "end": self.end_date}
                if self.start_date and self.end_date
                else None
            ),
            "available_metrics": self.available_metrics,
            "missing_fields": self.missing_fields,
            "missing_values": self.missing_values,
            "duplicate_dates": self.duplicate_dates,
            "missing_dates": self.missing_dates,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
            "issues": [i.to_dict() for i in self.issues],
        }


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_number(raw: str, field_name: str) -> float | None:
    value = raw.strip()
    if value == "":
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if field_name in INTEGER_METRICS and number != int(number):
        return None
    return number


def validate_csv(text: str, *, min_days: int = 14) -> ValidationReport:
    report = ValidationReport()

    try:
        frame = pd.read_csv(
            io.StringIO(text),
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            skip_blank_lines=True,
        )
    except pd.errors.EmptyDataError:
        report.issues.append(Issue("empty_file", "The CSV file contains no header or rows."))
        return report
    except pd.errors.ParserError as exc:
        report.issues.append(Issue("parse_error", f"The CSV file could not be parsed: {exc}"))
        return report

    frame.columns = [str(c).strip() for c in frame.columns]
    report.columns = list(frame.columns)
    report.row_count = int(len(frame))

    if report.row_count == 0:
        report.issues.append(Issue("empty_file", "The CSV file has a header but no data rows."))
        return report

    report.missing_fields = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    for missing in report.missing_fields:
        report.issues.append(
            Issue("missing_column", f"Required column '{missing}' is missing.", field=missing)
        )
    report.available_metrics = [c for c in METRIC_COLUMNS if c in frame.columns]

    extra_columns = [c for c in frame.columns if c not in REQUIRED_COLUMNS]
    for extra in extra_columns:
        report.issues.append(
            Issue(
                "extra_column",
                f"Column '{extra}' is not part of the health schema and was ignored.",
                field=extra,
                severity="warning",
            )
        )

    if not report.missing_fields:
        _validate_rows(frame, report, min_days=min_days)

    return report


def _validate_rows(frame: pd.DataFrame, report: ValidationReport, *, min_days: int) -> None:
    seen_dates: set[date] = set()
    clean_rows: list[dict[str, Any]] = []
    missing_values = {metric: 0 for metric in METRIC_COLUMNS}

    for index, row in frame.iterrows():
        line = int(index) + 2  # header is line 1
        row_ok = True

        raw_date = str(row.get("date", "")).strip()
        parsed_date = _parse_date(raw_date) if raw_date else None
        if parsed_date is None:
            code = "missing_value" if not raw_date else "invalid_date"
            report.issues.append(
                Issue(
                    code,
                    f"Row {line}: date value {raw_date!r} is not a valid YYYY-MM-DD date.",
                    field="date",
                    row=line,
                )
            )
            row_ok = False

        values: dict[str, float | None] = {}
        for metric in METRIC_COLUMNS:
            raw = str(row.get(metric, "")).strip()
            if raw == "":
                missing_values[metric] += 1
                report.issues.append(
                    Issue(
                        "missing_value",
                        f"Row {line}: '{metric}' is empty; it was not filled with an assumed value.",
                        field=metric,
                        row=line,
                    )
                )
                row_ok = False
                continue
            number = _parse_number(raw, metric)
            if number is None:
                report.issues.append(
                    Issue(
                        "non_numeric",
                        f"Row {line}: '{metric}' value {raw!r} is not a valid number.",
                        field=metric,
                        row=line,
                    )
                )
                row_ok = False
                continue
            low, high = RANGES[metric]
            if number < low or number > high:
                report.issues.append(
                    Issue(
                        "out_of_range",
                        f"Row {line}: '{metric}' value {number} is outside the allowed range "
                        f"[{low}, {high}].",
                        field=metric,
                        row=line,
                    )
                )
                row_ok = False
                continue
            plausible_low, plausible_high = PLAUSIBLE[metric]
            if number < plausible_low or number > plausible_high:
                report.issues.append(
                    Issue(
                        "implausible_value",
                        f"Row {line}: '{metric}' value {number} is unusual; please confirm it.",
                        field=metric,
                        row=line,
                        severity="warning",
                    )
                )
            values[metric] = number

        if parsed_date is not None:
            if parsed_date in seen_dates:
                report.duplicate_dates.append(parsed_date.isoformat())
                report.issues.append(
                    Issue(
                        "duplicate_date",
                        f"Row {line}: date {parsed_date.isoformat()} appears more than once.",
                        field="date",
                        row=line,
                    )
                )
                row_ok = False
            else:
                seen_dates.add(parsed_date)

        if row_ok and len(values) == len(METRIC_COLUMNS):
            clean_rows.append(
                {
                    "date": parsed_date.isoformat(),  # type: ignore[union-attr]
                    "sleep_hours": values["sleep_hours"],
                    "steps": int(values["steps"]),  # type: ignore[arg-type]
                    "active_minutes": int(values["active_minutes"]),  # type: ignore[arg-type]
                    "exercise_minutes": int(values["exercise_minutes"]),  # type: ignore[arg-type]
                    "resting_hr": int(values["resting_hr"]),  # type: ignore[arg-type]
                }
            )

    clean_rows.sort(key=lambda item: item["date"])
    report.clean_rows = clean_rows
    report.valid_row_count = len(clean_rows)
    report.missing_values = {k: v for k, v in missing_values.items() if v > 0}

    if clean_rows:
        report.start_date = clean_rows[0]["date"]
        report.end_date = clean_rows[-1]["date"]
        _check_coverage(clean_rows, report)
    else:
        report.issues.append(Issue("no_valid_rows", "No row passed data validation."))

    if report.row_count < min_days:
        report.issues.append(
            Issue(
                "insufficient_data",
                f"Only {report.row_count} data rows were provided; at least {min_days} days "
                "are required for a meaningful trend analysis.",
                severity="warning",
            )
        )
    elif report.valid_row_count < min_days:
        report.issues.append(
            Issue(
                "insufficient_data",
                f"Only {report.valid_row_count} valid rows are available; at least {min_days} "
                "days are required for a meaningful trend analysis.",
                severity="warning",
            )
        )


def _check_coverage(clean_rows: list[dict[str, Any]], report: ValidationReport) -> None:
    start = date.fromisoformat(clean_rows[0]["date"])
    end = date.fromisoformat(clean_rows[-1]["date"])
    present = {date.fromisoformat(row["date"]) for row in clean_rows}
    missing: list[str] = []
    cursor = start
    while cursor <= end:
        if cursor not in present:
            missing.append(cursor.isoformat())
        cursor += timedelta(days=1)
    report.missing_dates = missing
    if missing:
        report.issues.append(
            Issue(
                "calendar_gap",
                f"{len(missing)} calendar day(s) have no record between {start} and {end}: "
                f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}",
                severity="warning",
            )
        )


def validate_csv_bytes(content: bytes, *, min_days: int = 14) -> ValidationReport:
    return validate_csv(decode_csv_bytes(content), min_days=min_days)
