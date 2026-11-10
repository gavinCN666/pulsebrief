"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    goals_json TEXT NOT NULL DEFAULT '[]',
    preferences_json TEXT NOT NULL DEFAULT '{}',
    sleep_goal_hours REAL NOT NULL DEFAULT 7.5,
    daily_steps_goal INTEGER NOT NULL DEFAULT 8000,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    start_date TEXT,
    end_date TEXT,
    validation_json TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS health_rows (
    dataset_id TEXT NOT NULL,
    date TEXT NOT NULL,
    sleep_hours REAL,
    steps INTEGER,
    active_minutes INTEGER,
    exercise_minutes INTEGER,
    resting_hr INTEGER,
    PRIMARY KEY(dataset_id, date),
    FOREIGN KEY(dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    dataset_id TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    frequency TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    task_index INTEGER NOT NULL,
    target_metric TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(idempotency_key, task_index),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT,
    user_id TEXT NOT NULL,
    dataset_id TEXT,
    user_request TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    tokens_prompt INTEGER NOT NULL DEFAULT 0,
    tokens_completion INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS tool_traces (
    trace_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    tool_name TEXT,
    input_json TEXT,
    output_json TEXT,
    output_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT,
    duration_ms REAL NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT,
    decided_by TEXT,
    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, kind, key),
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS eval_runs (
    eval_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    results_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_runs_user ON agent_runs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_traces_run ON tool_traces(run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals(run_id, status);
CREATE INDEX IF NOT EXISTS idx_datasets_user ON datasets(user_id, created_at DESC);
"""


class Database:
    """Small SQLite wrapper: one short-lived connection per operation.

    The app is single-user and low-write; short-lived connections keep Streamlit
    reruns and background evaluation runs isolated without a global lock.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def reset(self) -> None:
        """Drop all application tables (demo convenience, never used in prod)."""
        tables = [
            "tool_traces",
            "approvals",
            "tasks",
            "agent_runs",
            "health_rows",
            "datasets",
            "memory_items",
            "eval_runs",
            "users",
        ]
        with self.connect() as conn:
            for table in tables:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.initialize()
