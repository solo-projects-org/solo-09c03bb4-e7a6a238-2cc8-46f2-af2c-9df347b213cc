from __future__ import annotations

from datetime import date, timedelta

import pytest


RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]
WINDOW = BEFORE + AFTER


def _readings(values: dict[date | str, float]) -> dict[str, object]:
    def _iso(key: date | str) -> str:
        return key.isoformat() if isinstance(key, date) else key

    return {
        "readings": [
            {"date": _iso(d), "kwh_per_lamp": v}
            for d, v in sorted(values.items(), key=lambda item: _iso(item[0]))
        ]
    }


def _flat(days, value: float) -> dict[date, float]:
    return {d: value for d in days}


def seed(client):
    """Target plus two valid controls, one future-retrofit control,
    one incomplete segment and one no-retrofit incomplete segment."""

    ids = {}
    for name, retrofit in [
        ("T-目标", RETROFIT),
        ("C1-对照", None),
        ("C2-对照", None),
        ("C3-后段末日后换灯", RETROFIT + timedelta(days=8)),
        ("X-缺数对照", None),
        ("Y-无读数未换灯", None),
    ]:
        resp = client.post(
            "/api/segments",
            json={"name": name, "retrofit_date": retrofit.isoformat() if retrofit else None},
        )
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]

    target = {d.isoformat(): 10.0 for d in BEFORE}
    target.update({d.isoformat(): 8.0 for d in AFTER})
    assert client.put(
        f"/api/segments/{ids['T-目标']}/readings", json=_readings(_to_date(target))
    ).status_code == 200

    for name, before, after in [
        ("C1-对照", 10.2, 9.7),
        ("C2-对照", 10.4, 9.9),
        ("C3-后段末日后换灯", 10.2, 9.7),
    ]:
        values = {**_flat(BEFORE, before), **_flat(AFTER, after)}
        assert client.put(
            f"/api/segments/{ids[name]}/readings", json=_readings(values)
        ).status_code == 200

    # X is eligible by date but lacks readings; Y has no readings and no retrofit.
    assert client.put(
        f"/api/segments/{ids['X-缺数对照']}/readings",
        json=_readings({**_flat(BEFORE, 7.0), **_flat(AFTER[:-1], 6.0)}),
    ).status_code == 200
    return ids


def _to_date(raw: dict[str, float]) -> dict[date, float]:
    return {date.fromisoformat(k): v for k, v in raw.items()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("LIGHTS_DB_PATH", str(db_path))
    app = create_app(Settings(db_path=str(db_path)))
    with TestClient(app) as c:
        yield c


def test_issue_report_uses_7_day_windows_controls_and_net_savings(client):
    ids = seed(client)
    resp = client.post("/api/reports", params={"segment_id": ids["T-目标"]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "issued"
    report = body["report"]
    assert report["comparison"] == {
        "before_start": "2026-03-03",
        "before_end": "2026-03-09",
        "after_start": "2026-03-11",
        "after_end": "2026-03-17",
        "skipped_date": "2026-03-10",
    }
    assert report["target"]["reduction"] == pytest.approx(2.0)
    assert report["control_count"] == 3
    assert [c["name"] for c in report["controls"]] == [
        "C1-对照",
        "C2-对照",
        "C3-后段末日后换灯",
    ]
    for control in report["controls"]:
        assert control["reduction"] == pytest.approx(0.5)
        assert len(control["readings"]["before"]) == 7
        assert len(control["readings"]["after"]) == 7
        assert RETROFIT not in [date.fromisoformat(p["date"]) for p in control["readings"]["before"]]
    assert report["control_avg_reduction"] == pytest.approx(0.5)
    assert report["net_savings_per_lamp_per_day"] == pytest.approx(1.5)
    assert report["status"] == "issued"

    stored = client.get(f"/api/reports/{report['id']}").json()
    assert stored == report


def test_deferred_when_target_readings_missing(client):
    ids = seed(client)
    target_id = ids["T-目标"]
    existing = client.get(f"/api/segments/{target_id}/readings").json()["readings"]
    kept = [r for r in existing if r["date"] != "2026-03-15"]
    # PUT upserts; delete one day directly through the DB layer to simulate a gap.
    from app.db import Database
    from app.config import load_settings

    db = Database(load_settings().db_path)
    with db.write() as conn:
        conn.execute(
            "DELETE FROM readings WHERE segment_id = ? AND reading_date = ?",
            (target_id, "2026-03-15"),
        )
    resp = client.post("/api/reports", params={"segment_id": target_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deferred"
    assert body["report"] is None
    assert body["reasons"][0]["type"] == "target_missing_readings"
    assert body["reasons"][0]["dates"] == ["2026-03-15"]
    assert kept  # sanity


def test_deferred_without_controls(client):
    seg = client.post(
        "/api/segments",
        json={"name": "Lonely", "retrofit_date": RETROFIT.isoformat()},
    ).json()
    values = {**_flat(BEFORE, 10.0), **_flat(AFTER, 8.0)}
    client.put(f"/api/segments/{seg['id']}/readings", json=_readings(values))
    resp = client.post("/api/reports", params={"segment_id": seg["id"]})
    body = resp.json()
    assert body["status"] == "deferred"
    assert [r["type"] for r in body["reasons"]] == ["no_controls"]


def test_deferred_when_segment_not_retrofitted(client):
    seg = client.post("/api/segments", json={"name": "NoRetrofit"}).json()
    resp = client.post("/api/reports", params={"segment_id": seg["id"]})
    body = resp.json()
    assert body["status"] == "deferred"
    assert body["reasons"][0]["type"] == "target_not_retrofitted"


def test_correction_marks_report_pending_and_preserves_original(client):
    ids = seed(client)
    report = client.post(
        "/api/reports", params={"segment_id": ids["T-目标"]}
    ).json()["report"]

    correction = client.put(
        f"/api/segments/{ids['C1-对照']}/readings",
        json=_readings({"2026-03-13": 9.2}),
    )
    assert correction.status_code == 200
    result = correction.json()
    assert result["changed"] is True
    assert result["changed_readings"] == ["2026-03-13"]
    assert result["affected_reports"] == [
        {
            "report_id": report["id"],
            "previous_status": "issued",
            "status": "pending_recalc",
        }
    ]
    assert result["impact"]["reports_marked_pending"] == 1
    assert result["impact"]["segments_reread"] == [ids["C1-对照"]]

    after = client.get(f"/api/reports/{report['id']}").json()
    assert after["status"] == "pending_recalc"
    assert after["stale_at"] is not None
    assert after["stale_segment_ids"] == [ids["C1-对照"]]
    assert after["stale_reasons"][0]["type"] == "control_readings_changed"
    assert after["stale_reasons"][0]["dates"] == ["2026-03-13"]
    # Original results remain readable and unchanged.
    assert after["net_savings_per_lamp_per_day"] == report["net_savings_per_lamp_per_day"]
    assert after["control_avg_reduction"] == report["control_avg_reduction"]
    assert after["controls"] == report["controls"]

    listing = client.get(
        "/api/reports", params={"status": "pending_recalc"}
    ).json()
    assert [r["id"] for r in listing] == [report["id"]]


def test_idempotent_correction_does_not_duplicate_markers(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    payload = _readings({"2026-03-13": 9.2})
    client.put(f"/api/segments/{ids['C1-对照']}/readings", json=payload)
    again = client.put(f"/api/segments/{ids['C1-对照']}/readings", json=payload).json()
    assert again["changed"] is False
    assert again["affected_reports"] == []
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["status"] == "pending_recalc"
    assert len(detail["stale_reasons"]) == 1


def test_retrofit_date_correction_marks_window_changed(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    result = client.patch(
        f"/api/segments/{ids['T-目标']}",
        json={"retrofit_date": (RETROFIT + timedelta(days=1)).isoformat()},
    ).json()
    assert result["changed"] is True
    assert result["old_retrofit_date"] == "2026-03-10"
    assert result["new_retrofit_date"] == "2026-03-11"
    assert [a["report_id"] for a in result["affected_reports"]] == [report["id"]]
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["status"] == "pending_recalc"
    assert detail["stale_reasons"][0]["type"] == "comparison_window_changed"
    assert detail["stale_reasons"][0]["old_window"]["skipped_date"] == "2026-03-10"


def test_clearing_retrofit_date_of_existing_control_has_no_effect(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    # C3 already qualified as a control (retrofit after the window), so clearing
    # its date does not change the control list or its readings.
    result = client.patch(
        f"/api/segments/{ids['C3-后段末日后换灯']}", json={"retrofit_date": None}
    ).json()
    assert result["affected_reports"] == []
    assert client.get(f"/api/reports/{report['id']}").json()["status"] == "issued"


def test_segment_that_becomes_complete_marks_control_added(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    values = {**_flat(BEFORE, 5.0), **_flat(AFTER, 4.5)}
    result = client.put(
        f"/api/segments/{ids['Y-无读数未换灯']}/readings", json=_readings(values)
    ).json()
    assert [a["report_id"] for a in result["affected_reports"]] == [report["id"]]
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["status"] == "pending_recalc"
    assert detail["stale_reasons"][0]["type"] == "control_added"


def test_control_retrofit_moved_into_window_marks_control_removed(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    result = client.patch(
        f"/api/segments/{ids['C1-对照']}",
        json={"retrofit_date": (RETROFIT + timedelta(days=2)).isoformat()},
    ).json()
    assert [a["report_id"] for a in result["affected_reports"]] == [report["id"]]
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["stale_reasons"][0]["type"] == "control_removed"
    assert detail["stale_reasons"][0]["reason"] == "retrofit_date_now_inside_window"


def test_correction_only_rereads_corrected_segment(client, monkeypatch):
    from app import service

    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]

    calls: list[int] = []
    original_for_dates = service._readings_for_dates
    original_all = service._all_readings

    def spy_for_dates(conn, segment_id, dates):
        calls.append(segment_id)
        return original_for_dates(conn, segment_id, dates)

    def spy_all(conn, segment_id):
        calls.append(segment_id)
        return original_all(conn, segment_id)

    monkeypatch.setattr(service, "_readings_for_dates", spy_for_dates)
    monkeypatch.setattr(service, "_all_readings", spy_all)

    client.put(
        f"/api/segments/{ids['C1-对照']}/readings",
        json=_readings({"2026-03-13": 9.2}),
    )
    assert set(calls) == {ids["C1-对照"]}
    assert client.get(f"/api/reports/{report['id']}").json()["status"] == "pending_recalc"


def test_unrelated_segment_correction_does_not_mark_report(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    # X is ineligible as a control (missing readings) and remains so.
    result = client.put(
        f"/api/segments/{ids['X-缺数对照']}/readings",
        json=_readings({"2026-03-18": 5.0}),
    ).json()
    assert result["affected_reports"] == []
    assert client.get(f"/api/reports/{report['id']}").json()["status"] == "issued"


def test_correction_interleaved_with_report_issuance_is_not_missed(client):
    """A correction racing report issuance must still mark the new report.

    SQLite ``BEGIN IMMEDIATE`` serialises the two writers.  The report's
    snapshot read is paused via a test hook while a correction enters another
    thread; the correction blocks until the report commits, then sees it.
    """

    import threading
    import time

    from app import service
    from app.main import app as _app  # noqa: F401  (ensures module loaded)

    ids = seed(client)
    entered = threading.Event()
    correction_done = threading.Event()
    outcome: dict[str, object] = {}

    def concurrent_correction():
        entered.wait(timeout=10)
        t0 = time.monotonic()
        resp = client.put(
            f"/api/segments/{ids['C1-对照']}/readings",
            json=_readings({"2026-03-13": 9.1}),
        )
        outcome["status_code"] = resp.status_code
        outcome["affected"] = resp.json()["affected_reports"]
        outcome["blocked_seconds"] = time.monotonic() - t0
        correction_done.set()

    thread = threading.Thread(target=concurrent_correction)
    thread.start()

    def hook():
        entered.set()
        # Give the correction thread time to reach sqlite and block.
        time.sleep(0.3)

    from app.db import Database
    from app.config import load_settings

    db = Database(load_settings().db_path)
    result = service.issue_report(db, ids["T-目标"], on_snapshot_read=hook)
    assert result["status"] == "issued"
    assert correction_done.wait(timeout=10)
    thread.join(timeout=10)

    assert outcome["status_code"] == 200
    assert outcome["affected"] == [
        {
            "report_id": result["report"]["id"],
            "previous_status": "issued",
            "status": "pending_recalc",
        }
    ]
    detail = client.get(f"/api/reports/{result['report']['id']}").json()
    assert detail["status"] == "pending_recalc"
    assert detail["stale_segment_ids"] == [ids["C1-对照"]]


def test_correction_before_report_commits_is_reflected_in_snapshot(client):
    """If a correction commits first, the new report must use the new value."""

    ids = seed(client)
    client.put(
        f"/api/segments/{ids['C1-对照']}/readings",
        json=_readings({"2026-03-13": 9.2}),
    )
    body = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()
    assert body["status"] == "issued"
    report = body["report"]
    c1 = next(c for c in report["controls"] if c["name"] == "C1-对照")
    after_point = next(
        p for p in c1["readings"]["after"] if p["date"] == "2026-03-13"
    )
    assert after_point["kwh_per_lamp"] == 9.2
    assert report["status"] == "issued"


def test_multiple_reports_are_all_marked(client):
    ids = seed(client)
    r1 = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    r2 = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    result = client.put(
        f"/api/segments/{ids['C1-对照']}/readings",
        json=_readings({"2026-03-13": 9.0}),
    ).json()
    assert {a["report_id"] for a in result["affected_reports"]} == {r1["id"], r2["id"]}
    for report_id in (r1["id"], r2["id"]):
        detail = client.get(f"/api/reports/{report_id}").json()
        assert detail["status"] == "pending_recalc"
        assert detail["net_savings_per_lamp_per_day"] == pytest.approx(1.5)


def test_report_validation_errors(client):
    assert client.post("/api/reports", params={"segment_id": 9999}).status_code == 404
    seg = client.post("/api/segments", json={"name": "dup"}).json()
    assert client.post("/api/segments", json={"name": "dup"}).status_code == 409
    assert client.put(
        f"/api/segments/{seg['id']}/readings",
        json={"readings": [{"date": "2026-03-01", "kwh_per_lamp": -1}]},
    ).status_code == 422
    assert client.patch(
        f"/api/segments/{seg['id']}", json={"retrofit_date": "not-a-date"}
    ).status_code == 422


def test_deleting_target_reading_marks_report_target_readings_changed(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    result = client.delete(
        f"/api/segments/{ids['T-目标']}/readings/2026-03-15"
    ).json()
    assert result["changed"] is True
    assert result["changed_readings"] == ["2026-03-15"]
    assert [a["report_id"] for a in result["affected_reports"]] == [report["id"]]
    detail = client.get(f"/api/reports/{report['id']}").json()
    assert detail["status"] == "pending_recalc"
    assert detail["stale_reasons"][0]["type"] == "target_readings_changed"
    assert detail["stale_reasons"][0]["dates"] == ["2026-03-15"]
    # Snapshot still readable.
    assert detail["net_savings_per_lamp_per_day"] == pytest.approx(1.5)


def test_delete_missing_reading_is_noop(client):
    ids = seed(client)
    result = client.delete(
        f"/api/segments/{ids['T-目标']}/readings/2025-01-01"
    ).json()
    assert result["changed"] is False
    assert result["affected_reports"] == []


def test_reading_outside_window_does_not_mark_report(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    result = client.put(
        f"/api/segments/{ids['C1-对照']}/readings",
        json=_readings({"2026-04-01": 3.3}),
    ).json()
    assert result["changed"] is True
    assert result["affected_reports"] == []
    assert client.get(f"/api/reports/{report['id']}").json()["status"] == "issued"


def test_reading_on_skipped_retrofit_day_does_not_affect_report(client):
    ids = seed(client)
    report = client.post("/api/reports", params={"segment_id": ids["T-目标"]}).json()["report"]
    result = client.put(
        f"/api/segments/{ids['C1-对照']}/readings",
        json=_readings({"2026-03-10": 123.0}),
    ).json()
    # The skipped day is never used by the comparison.
    assert result["affected_reports"] == []
    assert client.get(f"/api/reports/{report['id']}").json()["status"] == "issued"


def test_duplicate_dates_in_one_upsert_last_one_wins(client):
    seg = client.post("/api/segments", json={"name": "dupe-dates"}).json()
    payload = {
        "readings": [
            {"date": "2026-03-01", "kwh_per_lamp": 10.0},
            {"date": "2026-03-01", "kwh_per_lamp": 9.5},
            {"date": "2026-03-02", "kwh_per_lamp": 9.0},
        ]
    }
    result = client.put(f"/api/segments/{seg['id']}/readings", json=payload).json()
    assert result["changed"] is True
    assert result["changed_readings"] == ["2026-03-01", "2026-03-02"]
    stored = client.get(f"/api/segments/{seg['id']}/readings").json()["readings"]
    assert stored == [
        {"date": "2026-03-01", "kwh_per_lamp": 9.5},
        {"date": "2026-03-02", "kwh_per_lamp": 9.0},
    ]
