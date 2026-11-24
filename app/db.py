"""SQLite access helpers.

The whole service deliberately uses short, explicit transactions with
``BEGIN IMMEDIATE`` for every write, including report generation.  SQLite then
serialises report generation and corrections at the database level, which is
what guarantees that a correction can never interleave between the snapshot
read and the report insert (and therefore can never miss marking a report as
pending recalculation).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    retrofit_date  TEXT,
    version        INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    segment_id    INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    reading_date  TEXT NOT NULL,
    kwh_per_lamp  REAL NOT NULL CHECK (kwh_per_lamp >= 0),
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (segment_id, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_readings_date
    ON readings (reading_date);

-- Maintained alongside ``readings`` by the triggers below.  It answers "does a
-- daily reading exist for this segment/day?" without touching the readings
-- table, which lets report issuance batch-screen controls instead of reading
-- every candidate segment's consumption.
CREATE TABLE IF NOT EXISTS reading_coverage (
    segment_id    INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    reading_date  TEXT NOT NULL,
    PRIMARY KEY (segment_id, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_reading_coverage_date
    ON reading_coverage (reading_date);

-- One-off bookkeeping for data-volume upgrades (see Database._backfill_coverage).
CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- The coverage index is updated by the same SQL statement that changes the
-- reading, so it commits or rolls back with the reading transaction.
-- Maintenance therefore never needs to rescan a segment's reading history.
CREATE TRIGGER IF NOT EXISTS trg_readings_coverage_insert
AFTER INSERT ON readings
BEGIN
    INSERT OR IGNORE INTO reading_coverage (segment_id, reading_date)
    VALUES (NEW.segment_id, NEW.reading_date);
END;

CREATE TRIGGER IF NOT EXISTS trg_readings_coverage_delete
AFTER DELETE ON readings
BEGIN
    DELETE FROM reading_coverage
    WHERE segment_id = OLD.segment_id AND reading_date = OLD.reading_date;
END;

CREATE TRIGGER IF NOT EXISTS trg_readings_coverage_update
AFTER UPDATE OF reading_date, segment_id ON readings
BEGIN
    DELETE FROM reading_coverage
    WHERE segment_id = OLD.segment_id AND reading_date = OLD.reading_date;
    INSERT OR IGNORE INTO reading_coverage (segment_id, reading_date)
    VALUES (NEW.segment_id, NEW.reading_date);
END;

CREATE TABLE IF NOT EXISTS reports (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_segment_id               INTEGER NOT NULL REFERENCES segments(id),
    target_name                     TEXT NOT NULL,
    target_retrofit_date            TEXT NOT NULL,
    before_start                    TEXT NOT NULL,
    before_end                      TEXT NOT NULL,
    after_start                     TEXT NOT NULL,
    after_end                       TEXT NOT NULL,
    skipped_date                    TEXT NOT NULL,
    target_before_avg               REAL NOT NULL,
    target_after_avg                REAL NOT NULL,
    target_reduction                REAL NOT NULL,
    control_avg_reduction           REAL NOT NULL,
    net_savings_per_lamp_per_day    REAL NOT NULL,
    controls_json                   TEXT NOT NULL,
    snapshot_json                   TEXT NOT NULL,
    status                          TEXT NOT NULL
        CHECK (status IN ('issued', 'pending_recalc')),
    stale_at                        TEXT,
    stale_reasons_json              TEXT NOT NULL DEFAULT '[]',
    stale_segment_ids_json          TEXT NOT NULL DEFAULT '[]',
    created_at                      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_target
    ON reports (target_segment_id, created_at);
"""


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp with a ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


#: Marks a data volume whose ``reading_coverage`` has been populated from the
#: readings that already existed before the coverage index was introduced.
COVERAGE_BACKFILL_KEY = "reading_coverage_backfill_v1"


class Database:
    """Small wrapper around sqlite3 with explicit read/write transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        self._backfill_coverage()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # explicit transactions below
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # Keeps REPLACE-style writes (delete+insert under the hood) firing the
        # coverage delete trigger as well.  The service itself uses
        # ON CONFLICT DO UPDATE, which the INSERT trigger covers.
        conn.execute("PRAGMA recursive_triggers=ON")
        return conn

    def _backfill_coverage(self) -> None:
        """Populate the coverage index once for a pre-existing data volume.

        The marker row is written in the same transaction as the backfill, so a
        crash either leaves both missing (and the next start retries) or both
        present.  Later maintenance never rescans historical readings.
        """

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            done = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?", (COVERAGE_BACKFILL_KEY,)
            ).fetchone()
            if done is None:
                conn.execute(
                    "INSERT OR IGNORE INTO reading_coverage (segment_id, reading_date) "
                    "SELECT segment_id, reading_date FROM readings"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, 'done')",
                    (COVERAGE_BACKFILL_KEY,),
                )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
