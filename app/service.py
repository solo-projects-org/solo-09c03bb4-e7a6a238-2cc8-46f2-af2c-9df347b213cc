"""Core accounting logic.

Design notes
------------
* A comparison window is the 7 calendar days before the retrofit date and the
  7 calendar days after it.  The retrofit day itself is skipped.
* A segment is control-eligible for a window when it has no retrofit date yet or
  its retrofit date lies after the last day of the after-window, and it has a
  reading on every one of the 14 window days.
* Savings are ``(target before average - target after average) -
  mean(control before average - control after average)`` in kWh per lamp per
  day.
* An issued report is an immutable snapshot of the comparison, the control
  list and every reading it used.  Later corrections only change its status to
  ``pending_recalc``; the original numbers remain readable.
* Every write (including report issuance) runs in ``BEGIN IMMEDIATE`` so report
  generation and corrections are serialised by SQLite.  A correction therefore
  either sees the new report and marks it, or commits before the report is
  snapshotted.  It can never fall between the snapshot read and the insert.
* Correction impact is evaluated by re-reading only the corrected segment's
  daily readings.  Other segments are never re-read; the report snapshot tells
  us which of those readings the report actually used.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Callable, Iterable

from .db import Database, utcnow_iso
from .models import (
    AffectedReport,
    ComparisonWindow,
    ControlDetail,
    CorrectionResult,
    ReadingPoint,
    ReportDetail,
    ReportIssueResult,
    ReportSummary,
    TargetDetail,
)

BEFORE_DAYS = 7
AFTER_DAYS = 7


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------------------
# Date / window helpers (pure)
# ---------------------------------------------------------------------------


def comparison_window(retrofit_date: date) -> dict[str, date]:
    return {
        "before_start": retrofit_date - timedelta(days=BEFORE_DAYS),
        "before_end": retrofit_date - timedelta(days=1),
        "after_start": retrofit_date + timedelta(days=1),
        "after_end": retrofit_date + timedelta(days=AFTER_DAYS),
        "skipped_date": retrofit_date,
    }


def window_dates(window: dict[str, date]) -> list[date]:
    """All 14 dates used by the comparison, in chronological order."""

    return [
        window["before_start"] + timedelta(days=offset)
        for offset in range(BEFORE_DAYS)
    ] + [
        window["after_start"] + timedelta(days=offset)
        for offset in range(AFTER_DAYS)
    ]


def _window_from_row(row: sqlite3.Row) -> dict[str, date]:
    return {
        "before_start": date.fromisoformat(row["before_start"]),
        "before_end": date.fromisoformat(row["before_end"]),
        "after_start": date.fromisoformat(row["after_start"]),
        "after_end": date.fromisoformat(row["after_end"]),
        "skipped_date": date.fromisoformat(row["skipped_date"]),
    }


def _average(values: Iterable[float]) -> float:
    values = list(values)
    return round(sum(values) / len(values), 6)


def _window_averages(
    readings: dict[date, float], window: dict[str, date]
) -> tuple[float, float, float]:
    before = [readings[window["before_start"] + timedelta(days=i)] for i in range(BEFORE_DAYS)]
    after = [readings[window["after_start"] + timedelta(days=i)] for i in range(AFTER_DAYS)]
    before_avg = _average(before)
    after_avg = _average(after)
    return before_avg, after_avg, round(before_avg - after_avg, 6)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _segment_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "retrofit_date": (
            date.fromisoformat(row["retrofit_date"]) if row["retrofit_date"] else None
        ),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_segment_row(conn: sqlite3.Connection, segment_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"segment {segment_id} not found")
    return row


def _readings_for_dates(
    conn: sqlite3.Connection, segment_id: int, dates: Iterable[date]
) -> dict[date, float]:
    dates = sorted(set(dates))
    if not dates:
        return {}
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"SELECT reading_date, kwh_per_lamp FROM readings "
        f"WHERE segment_id = ? AND reading_date IN ({placeholders})",
        (segment_id, *[d.isoformat() for d in dates]),
    ).fetchall()
    return {date.fromisoformat(r["reading_date"]): r["kwh_per_lamp"] for r in rows}


def _all_readings(conn: sqlite3.Connection, segment_id: int) -> dict[date, float]:
    rows = conn.execute(
        "SELECT reading_date, kwh_per_lamp FROM readings WHERE segment_id = ? "
        "ORDER BY reading_date",
        (segment_id,),
    ).fetchall()
    return {date.fromisoformat(r["reading_date"]): r["kwh_per_lamp"] for r in rows}


def _is_control_eligible(retrofit_date: date | None, after_end: date) -> bool:
    return retrofit_date is None or retrofit_date > after_end


# ---------------------------------------------------------------------------
# Segments and readings
# ---------------------------------------------------------------------------


def create_segment(db: Database, name: str, retrofit_date: date | None) -> dict:
    now = utcnow_iso()
    with db.write() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO segments (name, retrofit_date, version, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (name, retrofit_date.isoformat() if retrofit_date else None, now, now),
            )
        except sqlite3.IntegrityError:
            raise ApiError(409, f"segment name {name!r} already exists")
        row = _get_segment_row(conn, int(cur.lastrowid))
        return _segment_dict(row)


def list_segments(db: Database) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute("SELECT * FROM segments ORDER BY id").fetchall()
        return [_segment_dict(r) for r in rows]


def get_segment(db: Database, segment_id: int) -> dict:
    with db.read() as conn:
        return _segment_dict(_get_segment_row(conn, segment_id))


def get_readings(
    db: Database, segment_id: int, start: date | None, end: date | None
) -> list[dict]:
    with db.read() as conn:
        _get_segment_row(conn, segment_id)
        clauses = ["segment_id = ?"]
        params: list[object] = [segment_id]
        if start is not None:
            clauses.append("reading_date >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("reading_date <= ?")
            params.append(end.isoformat())
        rows = conn.execute(
            f"SELECT reading_date, kwh_per_lamp FROM readings "
            f"WHERE {' AND '.join(clauses)} ORDER BY reading_date",
            params,
        ).fetchall()
        return [
            {
                "date": date.fromisoformat(r["reading_date"]),
                "kwh_per_lamp": r["kwh_per_lamp"],
            }
            for r in rows
        ]


def correct_retrofit_date(
    db: Database, segment_id: int, new_retrofit: date | None
) -> CorrectionResult:
    """Set/clear a segment's retrofit date and mark affected reports stale."""

    with db.write() as conn:
        row = _get_segment_row(conn, segment_id)
        old_raw = row["retrofit_date"]
        old_retrofit = date.fromisoformat(old_raw) if old_raw else None
        if old_retrofit == new_retrofit:
            return CorrectionResult(
                segment_id=segment_id,
                changed=False,
                old_retrofit_date=old_retrofit,
                new_retrofit_date=new_retrofit,
                impact=_impact_metrics(conn, segment_id, affected=[]),
            ).model_dump()

        now = utcnow_iso()
        conn.execute(
            "UPDATE segments SET retrofit_date = ?, version = version + 1, updated_at = ? "
            "WHERE id = ?",
            (new_retrofit.isoformat() if new_retrofit else None, now, segment_id),
        )
        affected = _apply_correction_impact(
            conn,
            segment_id=segment_id,
            segment_name=row["name"],
            retrofit_changed=True,
            old_retrofit=old_retrofit,
            new_retrofit=new_retrofit,
            changed_dates=[],
        )
        return CorrectionResult(
            segment_id=segment_id,
            changed=True,
            old_retrofit_date=old_retrofit,
            new_retrofit_date=new_retrofit,
            affected_reports=affected,
            impact=_impact_metrics(conn, segment_id, affected),
        ).model_dump()


def upsert_readings(
    db: Database, segment_id: int, readings: list[tuple[date, float]]
) -> CorrectionResult:
    """Insert/overwrite daily readings and mark affected reports stale.

    Only this segment's readings are (re-)read for impact analysis.
    """

    with db.write() as conn:
        row = _get_segment_row(conn, segment_id)
        old = _all_readings(conn, segment_id)
        # Later entries win for a duplicated date, matching the upsert order.
        latest: dict[date, float] = {}
        for reading_date, value in readings:
            latest[reading_date] = value
        changed_dates: list[date] = []
        now = utcnow_iso()
        for reading_date, value in latest.items():
            if reading_date not in old or old[reading_date] != value:
                changed_dates.append(reading_date)

        if not changed_dates:
            return CorrectionResult(
                segment_id=segment_id,
                changed=False,
                old_retrofit_date=(
                    date.fromisoformat(row["retrofit_date"])
                    if row["retrofit_date"]
                    else None
                ),
                new_retrofit_date=(
                    date.fromisoformat(row["retrofit_date"])
                    if row["retrofit_date"]
                    else None
                ),
                impact=_impact_metrics(conn, segment_id, affected=[]),
            ).model_dump()

        for reading_date, value in latest.items():
            conn.execute(
                "INSERT INTO readings (segment_id, reading_date, kwh_per_lamp, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(segment_id, reading_date) DO UPDATE SET "
                "kwh_per_lamp = excluded.kwh_per_lamp, updated_at = excluded.updated_at",
                (segment_id, reading_date.isoformat(), value, now),
            )
        conn.execute(
            "UPDATE segments SET version = version + 1, updated_at = ? WHERE id = ?",
            (now, segment_id),
        )

        retrofit = (
            date.fromisoformat(row["retrofit_date"]) if row["retrofit_date"] else None
        )
        changed_dates_sorted = sorted(set(changed_dates))
        affected = _apply_correction_impact(
            conn,
            segment_id=segment_id,
            segment_name=row["name"],
            retrofit_changed=False,
            old_retrofit=retrofit,
            new_retrofit=retrofit,
            changed_dates=changed_dates_sorted,
        )
        return CorrectionResult(
            segment_id=segment_id,
            changed=True,
            changed_readings=changed_dates_sorted,
            old_retrofit_date=retrofit,
            new_retrofit_date=retrofit,
            affected_reports=affected,
            impact=_impact_metrics(conn, segment_id, affected),
        ).model_dump()


def delete_reading(
    db: Database, segment_id: int, reading_date: date
) -> CorrectionResult:
    """Delete one erroneous daily reading and mark affected reports stale."""

    with db.write() as conn:
        row = _get_segment_row(conn, segment_id)
        exists = conn.execute(
            "SELECT 1 FROM readings WHERE segment_id = ? AND reading_date = ?",
            (segment_id, reading_date.isoformat()),
        ).fetchone()
        retrofit = (
            date.fromisoformat(row["retrofit_date"]) if row["retrofit_date"] else None
        )
        if exists is None:
            return CorrectionResult(
                segment_id=segment_id,
                changed=False,
                old_retrofit_date=retrofit,
                new_retrofit_date=retrofit,
                impact=_impact_metrics(conn, segment_id, affected=[]),
            ).model_dump()

        conn.execute(
            "DELETE FROM readings WHERE segment_id = ? AND reading_date = ?",
            (segment_id, reading_date.isoformat()),
        )
        conn.execute(
            "UPDATE segments SET version = version + 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), segment_id),
        )
        affected = _apply_correction_impact(
            conn,
            segment_id=segment_id,
            segment_name=row["name"],
            retrofit_changed=False,
            old_retrofit=retrofit,
            new_retrofit=retrofit,
            changed_dates=[reading_date],
        )
        return CorrectionResult(
            segment_id=segment_id,
            changed=True,
            changed_readings=[reading_date],
            old_retrofit_date=retrofit,
            new_retrofit_date=retrofit,
            affected_reports=affected,
            impact=_impact_metrics(conn, segment_id, affected),
        ).model_dump()


def _impact_metrics(
    conn: sqlite3.Connection, segment_id: int, affected: list[AffectedReport]
) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
    newly = sum(1 for a in affected if a.previous_status == "issued")
    already = len(affected) - newly
    return {
        "reports_reviewed": total,
        "reports_marked_pending": newly,
        "reports_already_pending": already,
        "reports_unaffected": total - len(affected),
        "segments_reread": [segment_id],
    }


# ---------------------------------------------------------------------------
# Correction impact
# ---------------------------------------------------------------------------


def _snapshot_controls(snapshot: dict) -> list[dict]:
    return snapshot.get("controls", [])


def _apply_correction_impact(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    segment_name: str,
    retrofit_changed: bool,
    old_retrofit: date | None,
    new_retrofit: date | None,
    changed_dates: list[date],
) -> list[AffectedReport]:
    """Compare the corrected segment against report snapshots.

    Reads only the corrected segment's readings.  Every impacted report is
    marked ``pending_recalc`` inside the caller's write transaction.
    """

    reports = conn.execute("SELECT * FROM reports ORDER BY id").fetchall()
    if not reports:
        return []

    # Gather only the dates the reports actually use, then read this segment once.
    needed: set[date] = set()
    for report in reports:
        needed.update(window_dates(_window_from_row(report)))
    current = _readings_for_dates(conn, segment_id, needed)

    affected: list[AffectedReport] = []
    for report in reports:
        snapshot = json.loads(report["snapshot_json"])
        window = _window_from_row(report)
        dates = window_dates(window)
        reason: dict | None = None

        if report["target_segment_id"] == segment_id:
            if retrofit_changed:
                reason = {
                    "type": "comparison_window_changed",
                    "segment_id": segment_id,
                    "segment_name": segment_name,
                    "old_retrofit_date": old_retrofit.isoformat() if old_retrofit else None,
                    "new_retrofit_date": new_retrofit.isoformat() if new_retrofit else None,
                    "old_window": _iso_window(window),
                }
            else:
                used = snapshot["target"]["readings"]
                diffs = [
                    d
                    for d in dates
                    if str(d) in used and used[str(d)] != current.get(d)
                ]
                if diffs:
                    reason = {
                        "type": "target_readings_changed",
                        "segment_id": segment_id,
                        "segment_name": segment_name,
                        "dates": [d.isoformat() for d in diffs],
                    }
        else:
            controls = _snapshot_controls(snapshot)
            control = next((c for c in controls if c["id"] == segment_id), None)
            eligible = _is_control_eligible(new_retrofit, window["after_end"])
            has_all = all(d in current for d in dates)

            if control is not None:
                if not eligible or not has_all:
                    reason = {
                        "type": "control_removed",
                        "segment_id": segment_id,
                        "segment_name": segment_name,
                        "reason": (
                            "retrofit_date_now_inside_window"
                            if not eligible
                            else "readings_incomplete"
                        ),
                    }
                else:
                    diffs = [
                        d
                        for d in dates
                        if control["readings"].get(str(d)) != current.get(d)
                    ]
                    if diffs:
                        reason = {
                            "type": "control_readings_changed",
                            "segment_id": segment_id,
                            "segment_name": segment_name,
                            "dates": [d.isoformat() for d in diffs],
                        }
            elif eligible and has_all:
                # The segment was ineligible or incomplete when the report was
                # issued and now qualifies, so the control list would change.
                reason = {
                    "type": "control_added",
                    "segment_id": segment_id,
                    "segment_name": segment_name,
                }

        if reason is None:
            continue

        prev_status = report["status"]
        stale_at = report["stale_at"] or utcnow_iso()
        reasons = json.loads(report["stale_reasons_json"])
        reason["detected_at"] = utcnow_iso()
        if not any(
            _same_reason(existing, reason) for existing in reasons
        ):
            reasons.append(reason)
        segment_ids = json.loads(report["stale_segment_ids_json"])
        if segment_id not in segment_ids:
            segment_ids.append(segment_id)
        conn.execute(
            "UPDATE reports SET status = 'pending_recalc', stale_at = ?, "
            "stale_reasons_json = ?, stale_segment_ids_json = ? WHERE id = ?",
            (
                stale_at,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(segment_ids),
                report["id"],
            ),
        )
        affected.append(
            AffectedReport(
                report_id=report["id"],
                previous_status=prev_status,
                status="pending_recalc",
            )
        )

    # Every diff above is derived from the snapshot vs. the re-read values, so
    # stale marking relies only on this segment's readings, never on a
    # caller-supplied change list and never on re-reading other segments.
    _ = changed_dates
    return affected


def _same_reason(left: dict, right: dict) -> bool:
    keys = (set(left) | set(right)) - {"detected_at"}
    return all(left.get(k) == right.get(k) for k in keys)


def _iso_window(window: dict[str, date]) -> dict[str, str]:
    return {key: value.isoformat() for key, value in window.items()}


# ---------------------------------------------------------------------------
# Report issuance
# ---------------------------------------------------------------------------


def issue_report(
    db: Database,
    segment_id: int,
    *,
    on_snapshot_read: Callable[[], None] | None = None,
) -> dict:
    """Issue a report for a retrofitted segment, or defer.

    ``on_snapshot_read`` is a test seam used to force a correction to interleave
    between the snapshot read and the insert; production callers omit it.
    """

    with db.write() as conn:
        target_row = _get_segment_row(conn, segment_id)
        target_name = target_row["name"]
        target_retrofit = (
            date.fromisoformat(target_row["retrofit_date"])
            if target_row["retrofit_date"]
            else None
        )
        if target_retrofit is None:
            return ReportIssueResult(
                status="deferred",
                target_segment_id=segment_id,
                comparison=None,
                reasons=[
                    {
                        "type": "target_not_retrofitted",
                        "segment_id": segment_id,
                        "message": "target segment has no retrofit date",
                    }
                ],
            ).model_dump()

        window = comparison_window(target_retrofit)
        dates = window_dates(window)
        target_values = _readings_for_dates(conn, segment_id, dates)
        missing = [d for d in dates if d not in target_values]

        control_rows = conn.execute(
            "SELECT * FROM segments WHERE id <> ? AND "
            "(retrofit_date IS NULL OR retrofit_date > ?) ORDER BY name, id",
            (segment_id, window["after_end"].isoformat()),
        ).fetchall()

        controls: list[dict] = []
        for row in control_rows:
            values = _readings_for_dates(conn, row["id"], dates)
            if len(values) != len(dates):
                continue
            before_avg, after_avg, reduction = _window_averages(values, window)
            controls.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "retrofit_date": (
                        date.fromisoformat(row["retrofit_date"])
                        if row["retrofit_date"]
                        else None
                    ),
                    "before_avg": before_avg,
                    "after_avg": after_avg,
                    "reduction": reduction,
                    "readings": {str(d): values[d] for d in dates},
                }
            )

        if on_snapshot_read is not None:
            on_snapshot_read()

        reasons: list[dict] = []
        if missing:
            reasons.append(
                {
                    "type": "target_missing_readings",
                    "segment_id": segment_id,
                    "dates": [d.isoformat() for d in missing],
                }
            )
        if not controls:
            reasons.append(
                {
                    "type": "no_controls",
                    "segment_id": segment_id,
                    "message": "no segment qualifies as a control for this window",
                }
            )
        if reasons:
            return ReportIssueResult(
                status="deferred",
                target_segment_id=segment_id,
                comparison=ComparisonWindow(**window),
                report=None,
                reasons=reasons,
            ).model_dump()

        before_avg, after_avg, reduction = _window_averages(target_values, window)
        control_avg = _average(c["reduction"] for c in controls)
        net = round(reduction - control_avg, 6)

        snapshot = {
            "comparison": _iso_window(window),
            "target": {
                "id": segment_id,
                "name": target_name,
                "retrofit_date": target_retrofit.isoformat(),
                "before_avg": before_avg,
                "after_avg": after_avg,
                "reduction": reduction,
                "readings": {str(d): target_values[d] for d in dates},
            },
            "controls": [
                {
                    **{k: v for k, v in control.items() if k != "retrofit_date"},
                    "retrofit_date": (
                        control["retrofit_date"].isoformat()
                        if control["retrofit_date"]
                        else None
                    ),
                }
                for control in controls
            ],
            "control_avg_reduction": control_avg,
            "net_savings_per_lamp_per_day": net,
        }
        created_at = utcnow_iso()
        cur = conn.execute(
            "INSERT INTO reports ("
            "target_segment_id, target_name, target_retrofit_date, "
            "before_start, before_end, after_start, after_end, skipped_date, "
            "target_before_avg, target_after_avg, target_reduction, "
            "control_avg_reduction, net_savings_per_lamp_per_day, "
            "controls_json, snapshot_json, status, stale_at, "
            "stale_reasons_json, stale_segment_ids_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued', NULL, '[]', '[]', ?)",
            (
                segment_id,
                target_name,
                target_retrofit.isoformat(),
                window["before_start"].isoformat(),
                window["before_end"].isoformat(),
                window["after_start"].isoformat(),
                window["after_end"].isoformat(),
                window["skipped_date"].isoformat(),
                before_avg,
                after_avg,
                reduction,
                control_avg,
                net,
                json.dumps(
                    [
                        {"id": c["id"], "name": c["name"]}
                        for c in controls
                    ],
                    ensure_ascii=False,
                ),
                json.dumps(snapshot, ensure_ascii=False),
                created_at,
            ),
        )
        report_id = int(cur.lastrowid)
        report = _report_detail(conn, report_id)
        return ReportIssueResult(
            status="issued",
            target_segment_id=segment_id,
            comparison=ComparisonWindow(**window),
            report=report,
            reasons=[],
        ).model_dump()


# ---------------------------------------------------------------------------
# Report reading
# ---------------------------------------------------------------------------


def _report_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "status": row["status"],
        "target_segment_id": row["target_segment_id"],
        "target_name": row["target_name"],
        "target_retrofit_date": date.fromisoformat(row["target_retrofit_date"]),
        "comparison": ComparisonWindow(**_window_from_row(row)),
        "control_count": len(json.loads(row["controls_json"])),
        "control_avg_reduction": row["control_avg_reduction"],
        "net_savings_per_lamp_per_day": row["net_savings_per_lamp_per_day"],
        "created_at": row["created_at"],
        "stale_at": row["stale_at"],
    }


def _report_detail(conn: sqlite3.Connection, report_id: int) -> dict:
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"report {report_id} not found")
    snapshot = json.loads(row["snapshot_json"])
    window = _window_from_row(row)
    target_snap = snapshot["target"]
    target = TargetDetail(
        segment_id=target_snap["id"],
        name=target_snap["name"],
        retrofit_date=date.fromisoformat(target_snap["retrofit_date"]),
        before_avg=target_snap["before_avg"],
        after_avg=target_snap["after_avg"],
        reduction=target_snap["reduction"],
        readings=_readings_by_split(target_snap["readings"], window),
    )
    controls = [
        ControlDetail(
            segment_id=c["id"],
            name=c["name"],
            retrofit_date=(
                date.fromisoformat(c["retrofit_date"]) if c["retrofit_date"] else None
            ),
            before_avg=c["before_avg"],
            after_avg=c["after_avg"],
            reduction=c["reduction"],
            readings=_readings_by_split(c["readings"], window),
        )
        for c in snapshot["controls"]
    ]
    return ReportDetail(
        id=row["id"],
        status=row["status"],
        target_segment_id=row["target_segment_id"],
        target_name=row["target_name"],
        target_retrofit_date=date.fromisoformat(row["target_retrofit_date"]),
        comparison=ComparisonWindow(**window),
        target=target,
        controls=controls,
        control_count=len(controls),
        control_avg_reduction=row["control_avg_reduction"],
        net_savings_per_lamp_per_day=row["net_savings_per_lamp_per_day"],
        created_at=row["created_at"],
        stale_at=row["stale_at"],
        stale_reasons=json.loads(row["stale_reasons_json"]),
        stale_segment_ids=json.loads(row["stale_segment_ids_json"]),
    ).model_dump()


def _readings_by_split(
    readings: dict[str, float], window: dict[str, date]
) -> dict[str, list[ReadingPoint]]:
    result: dict[str, list[ReadingPoint]] = {"before": [], "after": []}
    for offset in range(BEFORE_DAYS):
        d = window["before_start"] + timedelta(days=offset)
        result["before"].append(
            ReadingPoint(date=d, kwh_per_lamp=readings[str(d)])
        )
    for offset in range(AFTER_DAYS):
        d = window["after_start"] + timedelta(days=offset)
        result["after"].append(
            ReadingPoint(date=d, kwh_per_lamp=readings[str(d)])
        )
    return result


def get_report(db: Database, report_id: int) -> dict:
    with db.read() as conn:
        return _report_detail(conn, report_id)


def list_reports(
    db: Database,
    *,
    target_segment_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if target_segment_id is not None:
        clauses.append("target_segment_id = ?")
        params.append(target_segment_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.read() as conn:
        rows = conn.execute(
            f"SELECT * FROM reports {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_report_summary(row) for row in rows]
