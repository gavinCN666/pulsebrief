"""Repository layer: all SQL lives here, domain objects live in ``models.py``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from storage.database import Database
from storage.models import (
    ActionTask,
    AgentRun,
    Approval,
    ApprovalStatus,
    DatasetRecord,
    DatasetStatus,
    EvalRecord,
    MemoryItem,
    TaskStatus,
    ToolTrace,
    UserProfile,
    utc_now_iso,
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, profile: UserProfile) -> UserProfile:
        now = utc_now_iso()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, display_name, goals_json, preferences_json,
                                   sleep_goal_hours, daily_steps_goal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    goals_json = excluded.goals_json,
                    preferences_json = excluded.preferences_json,
                    sleep_goal_hours = excluded.sleep_goal_hours,
                    daily_steps_goal = excluded.daily_steps_goal,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.user_id,
                    profile.display_name,
                    _dumps(profile.goals),
                    _dumps(profile.preferences),
                    profile.sleep_goal_hours,
                    profile.daily_steps_goal,
                    profile.created_at,
                    now,
                ),
            )
        return self.get(profile.user_id) or profile

    def get(self, user_id: str) -> UserProfile | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return UserProfile(
            user_id=row["user_id"],
            display_name=row["display_name"],
            goals=_loads(row["goals_json"], []),
            preferences=_loads(row["preferences_json"], {}),
            sleep_goal_hours=row["sleep_goal_hours"],
            daily_steps_goal=row["daily_steps_goal"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_all(self) -> list[UserProfile]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT user_id FROM users ORDER BY created_at").fetchall()
        return [p for row in rows if (p := self.get(row["user_id"])) is not None]


class DatasetRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, dataset: DatasetRecord, rows: Iterable[dict[str, Any]]) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO datasets (dataset_id, user_id, filename, status, row_count,
                                      start_date, end_date, validation_json, content_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset.dataset_id,
                    dataset.user_id,
                    dataset.filename,
                    dataset.status.value,
                    dataset.row_count,
                    dataset.start_date,
                    dataset.end_date,
                    _dumps(dataset.validation),
                    dataset.content_sha256,
                    dataset.created_at,
                ),
            )
            conn.executemany(
                """
                INSERT INTO health_rows (dataset_id, date, sleep_hours, steps,
                                         active_minutes, exercise_minutes, resting_hr)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.dataset_id,
                        row["date"],
                        row.get("sleep_hours"),
                        row.get("steps"),
                        row.get("active_minutes"),
                        row.get("exercise_minutes"),
                        row.get("resting_hr"),
                    )
                    for row in rows
                ],
            )

    def get(self, dataset_id: str) -> DatasetRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if row is None:
            return None
        return DatasetRecord(
            dataset_id=row["dataset_id"],
            user_id=row["user_id"],
            filename=row["filename"],
            status=DatasetStatus(row["status"]),
            row_count=row["row_count"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            validation=_loads(row["validation_json"], {}),
            content_sha256=row["content_sha256"],
            created_at=row["created_at"],
        )

    def list_all(self, user_id: str | None = None, limit: int = 50) -> list[DatasetRecord]:
        if user_id:
            sql = "SELECT dataset_id FROM datasets WHERE user_id = ? ORDER BY created_at DESC LIMIT ?"
            params: tuple[Any, ...] = (user_id, limit)
        else:
            sql = "SELECT dataset_id FROM datasets ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [d for row in rows if (d := self.get(row["dataset_id"])) is not None]

    def rows(self, dataset_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT date, sleep_hours, steps, active_minutes, exercise_minutes, resting_hr "
                "FROM health_rows WHERE dataset_id = ? ORDER BY date",
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dataframe(self, dataset_id: str) -> pd.DataFrame:
        rows = self.rows(dataset_id)
        columns = ["date", "sleep_hours", "steps", "active_minutes", "exercise_minutes", "resting_hr"]
        frame = pd.DataFrame(rows, columns=columns)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def delete(self, dataset_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM health_rows WHERE dataset_id = ?", (dataset_id,))
            conn.execute("DELETE FROM datasets WHERE dataset_id = ?", (dataset_id,))


class TaskRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert_batch(self, tasks: list[ActionTask]) -> None:
        with self.db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO tasks (task_id, user_id, run_id, dataset_id, title, description,
                                   frequency, duration_days, scheduled_date, status,
                                   idempotency_key, task_index, target_metric, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        t.task_id,
                        t.user_id,
                        t.run_id,
                        t.dataset_id,
                        t.title,
                        t.description,
                        t.frequency,
                        t.duration_days,
                        t.scheduled_date,
                        t.status.value,
                        t.idempotency_key,
                        t.task_index,
                        t.target_metric,
                        t.created_at,
                        t.updated_at,
                    )
                    for t in tasks
                ],
            )

    def get(self, task_id: str) -> ActionTask | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._to_model(row) if row else None

    def _to_model(self, row: Any) -> ActionTask:
        return ActionTask(
            task_id=row["task_id"],
            user_id=row["user_id"],
            run_id=row["run_id"],
            dataset_id=row["dataset_id"],
            title=row["title"],
            description=row["description"],
            frequency=row["frequency"],
            duration_days=row["duration_days"],
            scheduled_date=row["scheduled_date"],
            status=TaskStatus(row["status"]),
            idempotency_key=row["idempotency_key"],
            task_index=row["task_index"],
            target_metric=row["target_metric"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def by_batch(self, batch_key: str) -> list[ActionTask]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ? ORDER BY task_index", (batch_key,)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def by_run(self, run_id: str) -> list[ActionTask]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE run_id = ? ORDER BY task_index", (run_id,)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def list_all(self, user_id: str | None = None, status: str | None = None) -> list[ActionTask]:
        clauses, params = [], []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at DESC, task_index", tuple(params)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status.value, utc_now_iso(), task_id),
            )


class RunRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, run: AgentRun) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (run_id, task_id, user_id, dataset_id, user_request,
                                        status, error, state_json, tokens_prompt, tokens_completion,
                                        created_at, updated_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.task_id,
                    run.user_id,
                    run.dataset_id,
                    run.user_request,
                    run.status.value,
                    run.error,
                    "{}",
                    run.tokens_prompt,
                    run.tokens_completion,
                    run.created_at,
                    run.updated_at,
                    run.completed_at,
                ),
            )

    def _to_model(self, row: Any) -> AgentRun:
        return AgentRun(
            run_id=row["run_id"],
            task_id=row["task_id"],
            user_id=row["user_id"],
            dataset_id=row["dataset_id"],
            user_request=row["user_request"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            tokens_prompt=row["tokens_prompt"],
            tokens_completion=row["tokens_completion"],
        )

    def get(self, run_id: str) -> AgentRun | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._to_model(row) if row else None

    def get_state_json(self, run_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT state_json FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        return _loads(row["state_json"], {}) if row else {}

    def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        state: dict[str, Any] | None = None,
        error: str | None = None,
        completed_at: str | None = None,
        tokens_prompt: int | None = None,
        tokens_completion: int | None = None,
    ) -> None:
        sets = ["updated_at = ?"]
        params: list[Any] = [utc_now_iso()]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if state is not None:
            sets.append("state_json = ?")
            params.append(json.dumps(state, ensure_ascii=False))
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if completed_at is not None:
            sets.append("completed_at = ?")
            params.append(completed_at)
        if tokens_prompt is not None:
            sets.append("tokens_prompt = ?")
            params.append(tokens_prompt)
        if tokens_completion is not None:
            sets.append("tokens_completion = ?")
            params.append(tokens_completion)
        params.append(run_id)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE run_id = ?", tuple(params))

    def list_all(self, user_id: str | None = None, limit: int = 30) -> list[AgentRun]:
        if user_id:
            sql = "SELECT * FROM agent_runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?"
            params: tuple[Any, ...] = (user_id, limit)
        else:
            sql = "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with self.db.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._to_model(row) for row in rows]

    def latest(self, user_id: str | None = None) -> AgentRun | None:
        runs = self.list_all(user_id, limit=1)
        return runs[0] if runs else None


class TraceRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, trace: ToolTrace) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_traces (trace_id, run_id, step_index, action_type, tool_name,
                                         input_json, output_json, output_summary, status, error,
                                         duration_ms, retry_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.run_id,
                    trace.step_index,
                    trace.action_type.value,
                    trace.tool_name,
                    _dumps(trace.tool_input) if trace.tool_input is not None else None,
                    _dumps(trace.output) if trace.output is not None else None,
                    trace.output_summary,
                    trace.status.value,
                    trace.error,
                    trace.duration_ms,
                    trace.retry_count,
                    trace.created_at,
                ),
            )

    def list_for_run(self, run_id: str) -> list[ToolTrace]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_traces WHERE run_id = ? ORDER BY step_index, created_at", (run_id,)
            ).fetchall()
        return [
            ToolTrace(
                trace_id=row["trace_id"],
                run_id=row["run_id"],
                step_index=row["step_index"],
                action_type=row["action_type"],
                tool_name=row["tool_name"],
                tool_input=_loads(row["input_json"], None),
                output=_loads(row["output_json"], None),
                output_summary=row["output_summary"],
                status=row["status"],
                error=row["error"],
                duration_ms=row["duration_ms"],
                retry_count=row["retry_count"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def stats(self, run_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN action_type = 'TOOL' THEN 1 ELSE 0 END) AS tool_calls,
                       SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS errors,
                       SUM(retry_count) AS retries,
                       COALESCE(SUM(duration_ms), 0) AS duration_ms
                FROM tool_traces WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else {"total": 0, "tool_calls": 0, "errors": 0, "retries": 0, "duration_ms": 0}


class ApprovalRepository:
    def __init__(self, db: Database):
        self.db = db

    def create(self, approval: Approval) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (approval_id, run_id, tool_name, payload_hash, payload_json,
                                       status, reason, requested_at, expires_at, decided_at,
                                       consumed_at, decided_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.run_id,
                    approval.tool_name,
                    approval.payload_hash,
                    _dumps(approval.payload),
                    approval.status.value,
                    approval.reason,
                    approval.requested_at,
                    approval.expires_at,
                    approval.decided_at,
                    approval.consumed_at,
                    approval.decided_by,
                ),
            )

    def get(self, approval_id: str) -> Approval | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        return Approval(
            approval_id=row["approval_id"],
            run_id=row["run_id"],
            tool_name=row["tool_name"],
            payload_hash=row["payload_hash"],
            payload=_loads(row["payload_json"], {}),
            status=ApprovalStatus(row["status"]),
            reason=row["reason"],
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            consumed_at=row["consumed_at"],
            decided_by=row["decided_by"],
        )

    def for_run(self, run_id: str) -> list[Approval]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT approval_id FROM approvals WHERE run_id = ? ORDER BY requested_at", (run_id,)
            ).fetchall()
        return [a for row in rows if (a := self.get(row["approval_id"])) is not None]

    def pending_for_run(self, run_id: str) -> Approval | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT approval_id FROM approvals WHERE run_id = ? AND status = 'PENDING' "
                "ORDER BY requested_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self.get(row["approval_id"]) if row else None

    def list_for_user(self, user_id: str, limit: int = 100) -> list[Approval]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT a.approval_id FROM approvals a "
                "JOIN agent_runs r ON a.run_id = r.run_id "
                "WHERE r.user_id = ? ORDER BY a.requested_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [a for row in rows if (a := self.get(row["approval_id"])) is not None]

    def set_status(self, approval_id: str, status: ApprovalStatus, *, reason: str | None = None,
                   decided_by: str | None = None) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, reason = COALESCE(?, reason), decided_at = ?, "
                "decided_by = ? WHERE approval_id = ?",
                (status.value, reason, utc_now_iso(), decided_by, approval_id),
            )

    def consume(self, approval_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE approvals SET status = 'CONSUMED', consumed_at = ? WHERE approval_id = ?",
                (utc_now_iso(), approval_id),
            )


class MemoryRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, item: MemoryItem) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_items (memory_id, user_id, kind, key, value_json, source,
                                          confidence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, kind, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (
                    item.memory_id,
                    item.user_id,
                    item.kind,
                    item.key,
                    _dumps(item.value),
                    item.source,
                    item.confidence,
                    item.created_at,
                    item.updated_at,
                ),
            )

    def list_for_user(self, user_id: str) -> list[MemoryItem]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_items WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
            ).fetchall()
        return [
            MemoryItem(
                memory_id=row["memory_id"],
                user_id=row["user_id"],
                kind=row["kind"],
                key=row["key"],
                value=_loads(row["value_json"], None),
                source=row["source"],
                confidence=row["confidence"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]


class EvalRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, record: EvalRecord) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_runs (eval_id, started_at, finished_at, total, passed,
                                                 metrics_json, results_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.eval_id,
                    record.started_at,
                    record.finished_at,
                    record.total,
                    record.passed,
                    _dumps(record.metrics),
                    _dumps(record.results),
                ),
            )

    def latest(self) -> EvalRecord | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return EvalRecord(
            eval_id=row["eval_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            total=row["total"],
            passed=row["passed"],
            metrics=_loads(row["metrics_json"], {}),
            results=_loads(row["results_json"], []),
        )


@dataclass
class Repositories:
    """Facade wiring all repositories to a single database."""

    db: Database

    def __post_init__(self) -> None:
        self.users = UserRepository(self.db)
        self.datasets = DatasetRepository(self.db)
        self.tasks = TaskRepository(self.db)
        self.runs = RunRepository(self.db)
        self.traces = TraceRepository(self.db)
        self.approvals = ApprovalRepository(self.db)
        self.memory = MemoryRepository(self.db)
        self.evals = EvalRepository(self.db)

    @classmethod
    def open(cls, path: str) -> "Repositories":
        db = Database(path)
        db.initialize()
        return cls(db)
