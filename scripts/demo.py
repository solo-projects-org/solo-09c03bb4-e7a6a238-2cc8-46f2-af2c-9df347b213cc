#!/usr/bin/env python3
"""End-to-end demo: seed segments, issue a report, then correct data.

Usage (service already running, e.g. via docker compose):

    python scripts/demo.py --base-url http://127.0.0.1:8000

The same calls can be made from Swagger UI at ``/docs``; this script prints
each request/response pair so the numbers can be checked by hand.

It also demonstrates the maintained-daily-report screening: a segment with a
one-day gap is excluded from the control list, and the list changes after the
gap is filled and after a day is deleted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta

import httpx

RETROFIT = date(2026, 3, 10)
BEFORE = [RETROFIT - timedelta(days=i) for i in range(1, 8)]
AFTER = [RETROFIT + timedelta(days=i) for i in range(1, 8)]


def _readings(before: float, after: float) -> dict:
    return {
        "readings": [
            {"date": d.isoformat(), "kwh_per_lamp": before} for d in BEFORE
        ]
        + [{"date": d.isoformat(), "kwh_per_lamp": after} for d in AFTER]
    }


def _show(label: str, payload: dict) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _report_controls(client: httpx.Client, target_id: int) -> dict:
    resp = client.post("/api/reports", params={"segment_id": target_id})
    resp.raise_for_status()
    body = resp.json()
    if body["status"] != "issued":
        print("UNEXPECTED: report deferred", body, file=sys.stderr)
        raise SystemExit(1)
    report = body["report"]
    print(
        "report",
        report["id"],
        "controls:",
        [c["name"] for c in report["controls"]],
        "net:",
        report["net_savings_per_lamp_per_day"],
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--suffix", default=str(int(time.time())), help="unique suffix for names"
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    suffix = args.suffix

    with httpx.Client(base_url=base, timeout=20.0) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        print("health:", health.json())

        def create(name: str, retrofit: date | None) -> int:
            resp = client.post(
                "/api/segments",
                json={
                    "name": f"{name}-{suffix}",
                    "retrofit_date": retrofit.isoformat() if retrofit else None,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            print(f"created segment {body['id']}: {body['name']} retrofit={body['retrofit_date']}")
            return body["id"]

        target_id = create("目标路段", RETROFIT)
        control1 = create("对照路段一", None)
        control2 = create("对照路段二", None)
        future = create("对照路段三-后段末日后换灯", RETROFIT + timedelta(days=8))
        incomplete = create("缺一天对照-应被排除", None)

        client.put(f"/api/segments/{target_id}/readings", json=_readings(10.0, 8.0)).raise_for_status()
        client.put(f"/api/segments/{control1}/readings", json=_readings(10.2, 9.7)).raise_for_status()
        client.put(f"/api/segments/{control2}/readings", json=_readings(10.4, 9.9)).raise_for_status()
        client.put(f"/api/segments/{future}/readings", json=_readings(10.2, 9.7)).raise_for_status()
        partial = _readings(7.0, 6.0)
        partial["readings"] = partial["readings"][:-1]  # one missing window day
        client.put(f"/api/segments/{incomplete}/readings", json=partial).raise_for_status()

        _show("initial report issuance", {
            "controls": [c["name"] for c in _report_controls(client, target_id)["controls"]]
        })
        print("the one-day-gap segment is excluded by the maintained coverage index")

        # Filling the missing day makes it a control on the next issuance.
        filled = client.put(
            f"/api/segments/{incomplete}/readings",
            json={"readings": [{"date": "2026-03-17", "kwh_per_lamp": 6.0}]},
        )
        filled.raise_for_status()
        _show("fill the missing day", filled.json())
        report_after_fill = _report_controls(client, target_id)
        assert "缺一天对照" in " ".join(c["name"] for c in report_after_fill["controls"])
        print("filling one day added the segment to the control list")

        # Deleting one day removes it again, and marks the earlier report stale.
        deleted = client.delete(
            f"/api/segments/{control2}/readings/2026-03-13"
        )
        deleted.raise_for_status()
        _show("delete one day from control2", deleted.json())
        assert deleted.json()["affected_reports"], "expected a stale report"
        report_after_delete = _report_controls(client, target_id)
        assert all(c["segment_id"] != control2 for c in report_after_delete["controls"])
        print("deleting one day removed control2 from the control list")

        detail = client.get(f"/api/reports/{report_after_fill['id']}")
        detail.raise_for_status()
        after = detail.json()
        assert after["status"] == "pending_recalc", after["status"]
        assert after["net_savings_per_lamp_per_day"] == report_after_fill[
            "net_savings_per_lamp_per_day"
        ]
        print(
            "\nOK: earlier report is pending_recalc, original result still readable, "
            f"stale segment ids={after['stale_segment_ids']}"
        )

        listing = client.get("/api/reports", params={"status": "pending_recalc"})
        listing.raise_for_status()
        print("pending_recalc reports:", [r["id"] for r in listing.json()])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
