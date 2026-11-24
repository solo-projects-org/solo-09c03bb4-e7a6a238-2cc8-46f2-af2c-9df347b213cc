"""Tests for coverage-index screening, targeted maintenance, and upgrades.

These cover the behaviour changes on top of the baseline accounting tests:

* report issuance batch-screens control candidates from the maintained
  coverage index (no per-segment queries, no reads of unused candidates);
* maintenance only reads the submitted/changed days, never the segment's
  historical daily reports, and coverage commits/rolls back with the reading;
* an existing SQLite data volume keeps working after the upgrade and gains a
  complete coverage index without changing report snapshots.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]
WINDOW = BEFORE + AFTER


def _flat(days, value: float) -> dict[date, float]:
    return {d: value for d in days}


def _readings(values: dict[date | str, float]) -> dict:
    def _iso(key: date | str) -> str:
        return key.isoformat() if isinstance(key, date) else key

    return {
        "readings": [
            {"date": _iso(d), "kwh_per_lamp": v}
            for d, v in sorted(values.items(), key=lambda item: _iso(item[0]))
        ]
    }


def _create(client, name: str, retrofit: date | None) -> int:
    resp = client.post(
        "/api/segments",
        json={"name": name, "retrofit_date": retrofit.isoformat() if retrofit else None},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed(client):
    target = _create(client, "T", RETROFIT)
    client.put(
        f"/api/segments/{target}/readings",
        json=_readings({**_flat(BEFORE, 10.0), **_flat(AFTER, 8.0)}),
    )
    c1 = _create(client, "C1", None)
    client.put(
        f"/api/segments/{c1}/readings",
        json=_readings({**_flat(BEFORE, 10.2), **_flat(AFTER, 9.7)}),
    )
    c2 = _create(client, "C2", None)
    client.put(
        f"/api/segments/{c2}/readings",
        json=_readings({**_flat(BEFORE, 10.4), **_flat(AFTER, 9.9)}),
    )
    return target, c1, c2


def _db_path(client) -> str:
    return client.app.state.db.path


def test_issuance_batches_screening_and_only_reads_selected_controls(client, monkeypatch):
    """One coverage query per issuance; values read for selected controls only."""

    from app import service

    target, c1, c2 = _seed(client)
    # A candidate that lacks one window day is screened out by coverage alone.
    incomplete = _create(client, "INC", None)
    client.put(
        f"/api/segments/{incomplete}/readings",
        json=_readings({**_flat(BEFORE, 5.0), **_flat(AFTER[:-1], 4.0)}),
    )

    coverage_calls: list[int] = []
    reads: list[tuple[int, tuple]] = []
    original_screen = service._screen_control_segments
    original_for_dates = service._readings_for_dates
    original_for_segments = service._readings_for_segments

    def spy_screen(conn, segment_id, window):
        coverage_calls.append(segment_id)
        return original_screen(conn, segment_id, window)

    def spy_for_dates(conn, segment_id, dates):
        reads.append((segment_id, tuple(sorted(dates))))
        return original_for_dates(conn, segment_id, dates)

    def spy_for_segments(conn, segment_ids, dates):
        segment_ids = list(segment_ids)
        reads.extend((sid, tuple(sorted(dates))) for sid in segment_ids)
        return original_for_segments(conn, segment_ids, dates)

    monkeypatch.setattr(service, "_screen_control_segments", spy_screen)
    monkeypatch.setattr(service, "_readings_for_dates", spy_for_dates)
    monkeypatch.setattr(service, "_readings_for_segments", spy_for_segments)

    body = client.post("/api/reports", params={"segment_id": target}).json()
    assert body["status"] == "issued", body
    assert coverage_calls == [target]  # exactly one batched screening per issuance

    read_segment_ids = [sid for sid, _ in reads]
    assert read_segment_ids.count(target) == 1  # target values
    # Only the two selected controls' values are read; the incomplete candidate
    # is never read, and no segment is read once per candidate query style.
    assert read_segment_ids.count(c1) == 1
    assert read_segment_ids.count(c2) == 1
    assert incomplete not in read_segment_ids
    # Value reads are limited to the 14 comparison dates.
    for _, dates in reads:
        assert set(dates) == set(WINDOW)


def test_issuance_does_not_query_readings_per_candidate(client, monkeypatch):
    """Prepare many candidates; screening stays one coverage query total."""

    from app import service

    target, c1, c2 = _seed(client)
    # 40 extra eligible candidates with full windows, all unused if the target
    # window has more controls than the report needs — every one is a control,
    # but screening must still be a single query.
    for i in range(40):
        sid = _create(client, f"BULK-{i}", None)
        client.put(
            f"/api/segments/{sid}/readings",
            json=_readings({**_flat(BEFORE, 10.0), **_flat(AFTER, 9.5)}),
        )

    statements: list[str] = []
    from app.db import Database

    original_connect = Database.connect

    def spy_connect(self):
        conn = original_connect(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "connect", spy_connect)
    body = client.post("/api/reports", params={"segment_id": target}).json()
    assert body["status"] == "issued", body
    assert body["report"]["control_count"] == 42

    # The readings table is read by exactly two SELECTs during issuance: the
    # target's window and one batched query for all selected controls.  There
    # is no per-candidate SELECT and no COUNT probe over readings.
    readings_selects = [
        s
        for s in statements
        if "FROM readings" in s and s.lstrip().upper().startswith("SELECT")
    ]
    assert len(readings_selects) == 2, readings_selects
    assert all("IN (" in s for s in readings_selects)


def test_upsert_only_reads_submitted_days_not_history(client, monkeypatch):
    from app import service

    target, c1, c2 = _seed(client)
    report = client.post("/api/reports", params={"segment_id": target}).json()["report"]

    # Give C1 a long history well outside the comparison window.
    history = {RETROFIT - timedelta(days=400 + i): 99.0 for i in range(60)}
    from app.db import Database

    db = Database(_db_path(client))
    now = "2026-01-01T00:00:00Z"
    with db.write() as conn:
        for d, v in history.items():
            conn.execute(
                "INSERT INTO readings (segment_id, reading_date, kwh_per_lamp, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (c1, d.isoformat(), v, now),
            )

    seen: list[tuple[int, tuple[date, ...]]] = []
    original = service._readings_for_dates

    def spy(conn, segment_id, dates):
        seen.append((segment_id, tuple(sorted(dates))))
        return original(conn, segment_id, dates)

    monkeypatch.setattr(service, "_readings_for_dates", spy)

    result = client.put(
        f"/api/segments/{c1}/readings", json=_readings({"2026-03-13": 9.0})
    ).json()
    assert result["changed"] is True
    assert [sid for sid, _ in seen] == [c1]
    # Only the submitted day is read back, never the 60-day backlog.
    assert all(dates == (date(2026, 3, 13),) for _, dates in seen)
    assert client.get(f"/api/reports/{report['id']}").json()["status"] == "pending_recalc"


def test_upsert_does_not_scan_history_with_sql_probe(client, monkeypatch):
    from app.db import Database

    target, c1, c2 = _seed(client)
    client.post("/api/reports", params={"segment_id": target})

    statements: list[str] = []
    original_connect = Database.connect

    def spy_connect(self):
        conn = original_connect(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "connect", spy_connect)
    client.put(f"/api/segments/{c1}/readings", json=_readings({"2026-03-13": 9.0}))

    # No statement may scan a segment's readings outside the submitted dates.
    forbidden = [
        s
        for s in statements
        if "FROM readings" in s
        and "reading_date IN" not in s
        and "reading_date >=" not in s
        and "reading_date <=" not in s
    ]
    assert forbidden == [], forbidden


def test_delete_commits_coverage_with_readings(client):
    target, c1, c2 = _seed(client)
    report = client.post("/api/reports", params={"segment_id": target}).json()["report"]

    db = sqlite3.connect(_db_path(client))
    try:
        covered = db.execute(
            "SELECT COUNT(*) FROM reading_coverage WHERE segment_id = ?", (c1,)
        ).fetchone()[0]
        assert covered == 14
    finally:
        db.close()

    client.delete(f"/api/segments/{c1}/readings/2026-03-13")

    db = sqlite3.connect(_db_path(client))
    try:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM reading_coverage "
                "WHERE segment_id = ? AND reading_date = '2026-03-13'",
                (c1,),
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()

    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["status"] == "pending_recalc"
    assert detail["stale_reasons"][0]["type"] == "control_removed"
    assert detail["stale_reasons"][0]["reason"] == "readings_incomplete"


def test_failed_reading_write_rolls_back_coverage(client):
    """A rejected reading leaves neither the value nor its coverage marker."""

    target, c1, c2 = _seed(client)
    db = sqlite3.connect(_db_path(client))
    try:
        before = db.execute(
            "SELECT COUNT(*) FROM reading_coverage WHERE segment_id = ?", (c1,)
        ).fetchone()[0]
    finally:
        db.close()

    from app.db import Database

    database = Database(_db_path(client))
    with pytest.raises(sqlite3.IntegrityError):
        with database.write() as conn:
            conn.execute(
                "INSERT INTO readings (segment_id, reading_date, kwh_per_lamp, updated_at) "
                "VALUES (?, '2026-03-20', -1, 'x')",
                (c1,),
            )

    db = sqlite3.connect(_db_path(client))
    try:
        after = db.execute(
            "SELECT COUNT(*) FROM reading_coverage WHERE segment_id = ?", (c1,)
        ).fetchone()[0]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM readings "
                "WHERE segment_id = ? AND reading_date = '2026-03-20'",
                (c1,),
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()
    assert after == before


def test_large_upsert_beyond_bound_variable_limit(client):
    """A >999-day upsert stays within SQLite's variable limit via chunking."""

    seg = _create(client, "MANY", None)
    start = date(2020, 1, 1)
    values = {start + timedelta(days=i): 1.0 + (i % 5) for i in range(1500)}
    resp = client.put(
        f"/api/segments/{seg}/readings", json=_readings(values)
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["changed_readings"]) == 1500

    stored = client.get(f"/api/segments/{seg}/readings").json()["readings"]
    assert len(stored) == 1500
    assert stored[-1]["date"] == (start + timedelta(days=1499)).isoformat()


def _legacy_schema() -> str:
    """The exact pre-upgrade schema, without reading_coverage/schema_meta."""

    return """
    CREATE TABLE segments (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        name           TEXT NOT NULL UNIQUE,
        retrofit_date  TEXT,
        version        INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );

    CREATE TABLE readings (
        segment_id    INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
        reading_date  TEXT NOT NULL,
        kwh_per_lamp  REAL NOT NULL CHECK (kwh_per_lamp >= 0),
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (segment_id, reading_date)
    );

    CREATE INDEX idx_readings_date ON readings (reading_date);

    CREATE TABLE reports (
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

    CREATE INDEX idx_reports_target ON reports (target_segment_id, created_at);
    """


def test_existing_sqlite_volume_upgrades_in_place(tmp_path, monkeypatch):
    """A legacy data file is upgraded without losing or altering its content."""

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    try:
        legacy.executescript(_legacy_schema())
        now = "2026-01-01T00:00:00Z"
        segments = [
            (1, "T-OLD", RETROFIT.isoformat(), 0, now, now),
            (2, "C-OLD", None, 0, now, now),
        ]
        legacy.executemany(
            "INSERT INTO segments (id, name, retrofit_date, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            segments,
        )
        rows = []
        for sid, before, after in [(1, 10.0, 8.0), (2, 10.2, 9.7)]:
            for d in BEFORE:
                rows.append((sid, d.isoformat(), before, now))
            for d in AFTER:
                rows.append((sid, d.isoformat(), after, now))
        legacy.executemany(
            "INSERT INTO readings (segment_id, reading_date, kwh_per_lamp, updated_at) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        snapshot = {
            "comparison": {
                "before_start": "2026-03-03",
                "before_end": "2026-03-09",
                "after_start": "2026-03-11",
                "after_end": "2026-03-17",
                "skipped_date": "2026-03-10",
            },
            "target": {
                "id": 1,
                "name": "T-OLD",
                "retrofit_date": "2026-03-10",
                "before_avg": 10.0,
                "after_avg": 8.0,
                "reduction": 2.0,
                "readings": {
                    **{d.isoformat(): 10.0 for d in BEFORE},
                    **{d.isoformat(): 8.0 for d in AFTER},
                },
            },
            "controls": [
                {
                    "id": 2,
                    "name": "C-OLD",
                    "retrofit_date": None,
                    "before_avg": 10.2,
                    "after_avg": 9.7,
                    "reduction": 0.5,
                    "readings": {
                        **{d.isoformat(): 10.2 for d in BEFORE},
                        **{d.isoformat(): 9.7 for d in AFTER},
                    },
                }
            ],
            "control_avg_reduction": 0.5,
            "net_savings_per_lamp_per_day": 1.5,
        }
        legacy.execute(
            "INSERT INTO reports ("
            "target_segment_id, target_name, target_retrofit_date, before_start, before_end, "
            "after_start, after_end, skipped_date, target_before_avg, target_after_avg, "
            "target_reduction, control_avg_reduction, net_savings_per_lamp_per_day, "
            "controls_json, snapshot_json, status, stale_at, stale_reasons_json, "
            "stale_segment_ids_json, created_at"
            ") VALUES (1, 'T-OLD', '2026-03-10', '2026-03-03', '2026-03-09', '2026-03-11', "
            "'2026-03-17', '2026-03-10', 10.0, 8.0, 2.0, 0.5, 1.5, ?, ?, 'issued', NULL, "
            "'[]', '[]', ?)",
            (json.dumps([{"id": 2, "name": "C-OLD"}]), json.dumps(snapshot), now),
        )
        legacy.commit()
    finally:
        legacy.close()

    monkeypatch.setenv("LIGHTS_DB_PATH", str(db_path))
    # Constructing the app runs the upgrade: creates coverage, backfills it.
    app = create_app(Settings(db_path=str(db_path)))
    with TestClient(app) as c:
        # Legacy content survived untouched, including the snapshot.
        detail = c.get("/api/reports/1").json()
        assert detail["net_savings_per_lamp_per_day"] == 1.5
        assert detail["status"] == "issued"
        assert detail["controls"][0]["name"] == "C-OLD"

        # The backfilled coverage index makes issuance work immediately.
        body = c.post("/api/reports", params={"segment_id": 1}).json()
        assert body["status"] == "issued", body
        assert body["report"]["control_count"] == 1
        assert body["report"]["net_savings_per_lamp_per_day"] == pytest.approx(1.5)

        # New writes keep coverage in sync.
        corr = c.put(
            "/api/segments/2/readings", json=_readings({"2026-03-13": 9.0})
        ).json()
        assert corr["changed"] is True
        assert {a["report_id"] for a in corr["affected_reports"]} == {1, 2}

    # Restarting must not duplicate or lose coverage.
    app2 = create_app(Settings(db_path=str(db_path)))
    with TestClient(app2) as c:
        con = sqlite3.connect(db_path)
        try:
            count = con.execute("SELECT COUNT(*) FROM reading_coverage").fetchone()[0]
            marker = con.execute(
                "SELECT value FROM schema_meta WHERE key = 'reading_coverage_backfill_v1'"
            ).fetchone()
        finally:
            con.close()
        assert count == 28
        assert marker == ("done",)
        assert c.get("/api/reports/1").json()["status"] == "pending_recalc"


def test_backfill_marks_legacy_volume_even_when_readings_empty(tmp_path):
    from app.db import COVERAGE_BACKFILL_KEY, Database

    db_path = tmp_path / "empty-legacy.db"
    legacy = sqlite3.connect(db_path)
    try:
        legacy.executescript(_legacy_schema())
        legacy.commit()
    finally:
        legacy.close()

    Database(db_path)
    con = sqlite3.connect(db_path)
    try:
        assert (
            con.execute(
                "SELECT value FROM schema_meta WHERE key = ?", (COVERAGE_BACKFILL_KEY,)
            ).fetchone()[0]
            == "done"
        )
    finally:
        con.close()
