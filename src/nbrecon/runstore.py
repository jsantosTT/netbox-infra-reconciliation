"""Local run history.

Needed for the one rule that spans runs: after N consecutive unreachable runs
the report suggests considering the device Offline. Status itself is human-only
and is never written by the tool.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .models import CollectionStatus, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    scope       TEXT NOT NULL,
    device_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS device_observations (
    run_id      TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    identifier  TEXT NOT NULL,
    device_id   INTEGER,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, identifier)
);

CREATE INDEX IF NOT EXISTS idx_obs_identifier
    ON device_observations (identifier, observed_at DESC);

CREATE TABLE IF NOT EXISTS applied_changes (
    run_id     TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    device_id  INTEGER NOT NULL,
    field_key  TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    outcome    TEXT NOT NULL
);
"""


@dataclass
class RunStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_run(self, run_id: str, stage: str, scope: str, device_count: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO runs (run_id, started_at, stage, scope, device_count) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET stage=excluded.stage, "
                "device_count=excluded.device_count",
                (run_id, utcnow().isoformat(), stage, scope, device_count),
            )
            conn.commit()

    def record_observation(
        self,
        run_id: str,
        identifier: str,
        status: CollectionStatus,
        device_id: int | None = None,
        detail: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO device_observations "
                "(run_id, observed_at, identifier, device_id, status, detail) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, identifier) DO UPDATE SET "
                "status=excluded.status, detail=excluded.detail",
                (run_id, utcnow().isoformat(), identifier, device_id, status.value, detail),
            )
            conn.commit()

    def consecutive_unreachable(self, identifier: str, limit: int = 10) -> int:
        """How many of the most recent runs in a row saw this device unreachable.

        Counts back from the latest observation and stops at the first run where
        the device was reachable, so an intermittent host never accumulates.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT status FROM device_observations WHERE identifier = ? "
                "ORDER BY observed_at DESC LIMIT ?",
                (identifier, limit),
            ).fetchall()
        streak = 0
        for row in rows:
            if row["status"] in (
                CollectionStatus.UNREACHABLE.value,
                CollectionStatus.FAILED.value,
            ):
                streak += 1
            else:
                break
        return streak

    def record_applied_change(
        self,
        run_id: str,
        device_id: int,
        field_key: str,
        old_value: object,
        new_value: object,
        outcome: str,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO applied_changes "
                "(run_id, applied_at, device_id, field_key, old_value, new_value, outcome) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    utcnow().isoformat(),
                    device_id,
                    field_key,
                    None if old_value is None else str(old_value),
                    None if new_value is None else str(new_value),
                    outcome,
                ),
            )
            conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
