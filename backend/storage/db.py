from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    target_url TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'swarm',
    status TEXT NOT NULL DEFAULT 'running',
    config_json TEXT NOT NULL DEFAULT '{}',
    reset_url TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    first_verified_at TEXT,
    total_cost_usd REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    persona TEXT NOT NULL,
    goal TEXT NOT NULL,
    viewport TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    steps_used INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL,
    current_url TEXT NOT NULL DEFAULT '',
    last_action TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trace_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    input_value TEXT,
    thought TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    screenshot_path TEXT NOT NULL,
    som_screenshot_path TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trace_steps_worker
    ON trace_steps (run_id, worker_id, step_index);

CREATE TABLE IF NOT EXISTS suspects (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    persona TEXT NOT NULL,
    category TEXT NOT NULL,
    error_signature TEXT NOT NULL,
    title_hint TEXT NOT NULL,
    url TEXT NOT NULL,
    trigger_step_index INTEGER NOT NULL,
    trace_slice_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    console_logs_json TEXT NOT NULL DEFAULT '[]',
    network_logs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'Detected',
    repro_count INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_suspects_run_status
    ON suspects (run_id, status);

CREATE TABLE IF NOT EXISTS verification_attempts (
    id TEXT PRIMARY KEY,
    suspect_id TEXT NOT NULL,
    attempt_num INTEGER NOT NULL,
    reproduced INTEGER NOT NULL,
    matched_signature TEXT,
    video_path TEXT,
    screenshot_path TEXT,
    notes TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (suspect_id) REFERENCES suspects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bug_reports (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    primary_suspect_id TEXT NOT NULL,
    dedupe_group_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    github_issue_url TEXT,
    github_issue_number INTEGER,
    markdown_export_path TEXT,
    video_path TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
    FOREIGN KEY (primary_suspect_id) REFERENCES suspects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    component TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
"""


class Database:
    """Async SQLite repository configured for concurrent WAL reads and writes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first.")
        return self._conn

    async def create_run(
        self,
        target_url: str,
        mode: str = "swarm",
        config: dict[str, Any] | None = None,
        reset_url: str | None = None,
        run_id: str | None = None,
    ) -> str:
        rid = run_id or f"run_{uuid.uuid4().hex[:12]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO runs (id, target_url, mode, status, config_json, reset_url, started_at)
                VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (rid, target_url, mode, json.dumps(config or {}), reset_url, utc_now_iso()),
            )
            await self.conn.commit()
        return rid

    async def update_run_status(
        self,
        run_id: str,
        status: str,
        finished: bool = False,
        first_verified_at: str | None = None,
    ) -> None:
        finished_at = utc_now_iso() if finished else None
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE runs
                SET status = ?,
                    finished_at = COALESCE(?, finished_at),
                    first_verified_at = COALESCE(first_verified_at, ?)
                WHERE id = ?
                """,
                (status, finished_at, first_verified_at, run_id),
            )
            await self.conn.commit()

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def create_worker(
        self,
        run_id: str,
        persona: str,
        goal: str,
        viewport: str,
        max_steps: int,
        worker_id: str | None = None,
    ) -> str:
        wid = worker_id or f"wrk_{uuid.uuid4().hex[:10]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO workers (id, run_id, persona, goal, viewport, status, max_steps, started_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (wid, run_id, persona, goal, viewport, max_steps, utc_now_iso()),
            )
            await self.conn.commit()
        return wid

    async def update_worker(
        self,
        worker_id: str,
        *,
        status: str | None = None,
        steps_used: int | None = None,
        current_url: str | None = None,
        last_action: str | None = None,
        finished: bool = False,
    ) -> None:
        finished_at = utc_now_iso() if finished else None
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE workers
                SET status = COALESCE(?, status),
                    steps_used = COALESCE(?, steps_used),
                    current_url = COALESCE(?, current_url),
                    last_action = COALESCE(?, last_action),
                    finished_at = COALESCE(?, finished_at)
                WHERE id = ?
                """,
                (status, steps_used, current_url, last_action, finished_at, worker_id),
            )
            await self.conn.commit()

    async def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def insert_trace_step(
        self,
        *,
        run_id: str,
        worker_id: str,
        step_index: int,
        action_type: str,
        locator: dict[str, Any],
        input_value: str | None,
        thought: str,
        url: str,
        screenshot_path: str,
        som_screenshot_path: str | None,
        duration_ms: int,
    ) -> str:
        step_id = f"stp_{uuid.uuid4().hex[:12]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO trace_steps (
                    id, run_id, worker_id, step_index, action_type, locator_json,
                    input_value, thought, url, screenshot_path, som_screenshot_path,
                    duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    run_id,
                    worker_id,
                    step_index,
                    action_type,
                    json.dumps(locator),
                    input_value,
                    thought,
                    url,
                    screenshot_path,
                    som_screenshot_path,
                    duration_ms,
                    utc_now_iso(),
                ),
            )
            await self.conn.commit()
        return step_id

    async def list_trace_steps(
        self, run_id: str, worker_id: str | None = None
    ) -> list[dict[str, Any]]:
        if worker_id:
            query = (
                "SELECT * FROM trace_steps WHERE run_id = ? AND worker_id = ? "
                "ORDER BY step_index ASC"
            )
            params: tuple[Any, ...] = (run_id, worker_id)
        else:
            query = (
                "SELECT * FROM trace_steps WHERE run_id = ? "
                "ORDER BY created_at ASC, step_index ASC"
            )
            params = (run_id,)
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def record_llm_usage(
        self,
        *,
        run_id: str,
        component: str,
        model: str,
        prompt_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        usage_id = f"llm_{uuid.uuid4().hex[:10]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO llm_usage (id, run_id, component, model, prompt_tokens, output_tokens, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (usage_id, run_id, component, model, prompt_tokens, output_tokens, cost_usd, utc_now_iso()),
            )
            await self.conn.execute(
                "UPDATE runs SET total_cost_usd = total_cost_usd + ? WHERE id = ?",
                (cost_usd, run_id),
            )
            await self.conn.commit()

    async def create_suspect(
        self,
        *,
        run_id: str,
        worker_id: str,
        persona: str,
        category: str,
        error_signature: str,
        title_hint: str,
        url: str,
        trigger_step_index: int,
        trace_slice: list[dict[str, Any]],
        evidence: dict[str, Any],
        console_logs: list[dict[str, Any]],
        network_logs: list[dict[str, Any]],
    ) -> str:
        suspect_id = f"sus_{uuid.uuid4().hex[:10]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO suspects (
                    id, run_id, worker_id, persona, category, error_signature, title_hint,
                    url, trigger_step_index, trace_slice_json, evidence_json,
                    console_logs_json, network_logs_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Detected', ?)
                """,
                (
                    suspect_id,
                    run_id,
                    worker_id,
                    persona,
                    category,
                    error_signature,
                    title_hint,
                    url,
                    trigger_step_index,
                    json.dumps(trace_slice),
                    json.dumps(evidence),
                    json.dumps(console_logs),
                    json.dumps(network_logs),
                    utc_now_iso(),
                ),
            )
            await self.conn.commit()
        return suspect_id

    async def update_suspect_status(
        self,
        suspect_id: str,
        status: str,
        *,
        repro_count: int | None = None,
        rejection_reason: str | None = None,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                UPDATE suspects
                SET status = ?,
                    repro_count = COALESCE(?, repro_count),
                    rejection_reason = COALESCE(?, rejection_reason)
                WHERE id = ?
                """,
                (status, repro_count, rejection_reason, suspect_id),
            )
            await self.conn.commit()

    async def list_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def list_workers(self, run_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM workers WHERE run_id = ? ORDER BY started_at ASC", (run_id,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def get_suspect(self, suspect_id: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM suspects WHERE id = ?", (suspect_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_suspects(self, run_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM suspects WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def insert_verification_attempt(
        self,
        *,
        suspect_id: str,
        attempt_num: int,
        reproduced: bool,
        matched_signature: str | None,
        video_path: str | None,
        screenshot_path: str | None,
        notes: str,
        duration_ms: int,
    ) -> str:
        att_id = f"att_{uuid.uuid4().hex[:10]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO verification_attempts (
                    id, suspect_id, attempt_num, reproduced, matched_signature,
                    video_path, screenshot_path, notes, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    att_id,
                    suspect_id,
                    attempt_num,
                    1 if reproduced else 0,
                    matched_signature,
                    video_path,
                    screenshot_path,
                    notes,
                    duration_ms,
                    utc_now_iso(),
                ),
            )
            await self.conn.commit()
        return att_id

    async def list_verification_attempts(self, suspect_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM verification_attempts WHERE suspect_id = ? ORDER BY attempt_num ASC",
            (suspect_id,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def insert_bug_report(
        self,
        *,
        run_id: str,
        primary_suspect_id: str,
        dedupe_group_id: str,
        title: str,
        severity: str,
        category: str,
        body_markdown: str,
        github_issue_url: str | None = None,
        github_issue_number: int | None = None,
        markdown_export_path: str | None = None,
        video_path: str | None = None,
    ) -> str:
        bug_id = f"bug_{uuid.uuid4().hex[:10]}"
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO bug_reports (
                    id, run_id, primary_suspect_id, dedupe_group_id, title, severity,
                    category, body_markdown, github_issue_url, github_issue_number,
                    markdown_export_path, video_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bug_id,
                    run_id,
                    primary_suspect_id,
                    dedupe_group_id,
                    title,
                    severity,
                    category,
                    body_markdown,
                    github_issue_url,
                    github_issue_number,
                    markdown_export_path,
                    video_path,
                    utc_now_iso(),
                ),
            )
            await self.conn.commit()
        return bug_id

    async def list_bug_reports(self, run_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM bug_reports WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def get_bug_report(self, bug_id: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM bug_reports WHERE id = ?", (bug_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_llm_usage(self, run_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM llm_usage WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

