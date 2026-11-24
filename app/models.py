"""Pydantic request/response models (also drive the Swagger schema)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReportStatus = Literal["issued", "pending_recalc"]


class SegmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    retrofit_date: date | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class SegmentPatch(BaseModel):
    # Deliberately required: sending {"retrofit_date": null} clears the date.
    retrofit_date: date | None


class ReadingInput(BaseModel):
    date: date
    kwh_per_lamp: float = Field(ge=0)


class ReadingsUpsert(BaseModel):
    readings: list[ReadingInput] = Field(min_length=1, max_length=10000)


class SegmentOut(BaseModel):
    id: int
    name: str
    retrofit_date: date | None
    version: int
    created_at: str
    updated_at: str


class ReadingPoint(BaseModel):
    date: date
    kwh_per_lamp: float


class ReadingsOut(BaseModel):
    segment_id: int
    readings: list[ReadingPoint]


class ComparisonWindow(BaseModel):
    before_start: date
    before_end: date
    after_start: date
    after_end: date
    skipped_date: date


class TargetDetail(BaseModel):
    segment_id: int
    name: str
    retrofit_date: date
    before_avg: float
    after_avg: float
    reduction: float
    readings: dict[str, list[ReadingPoint]]


class ControlDetail(BaseModel):
    segment_id: int
    name: str
    retrofit_date: date | None
    before_avg: float
    after_avg: float
    reduction: float
    readings: dict[str, list[ReadingPoint]]


class ReportDetail(BaseModel):
    id: int
    status: ReportStatus
    target_segment_id: int
    target_name: str
    target_retrofit_date: date
    comparison: ComparisonWindow
    target: TargetDetail
    controls: list[ControlDetail]
    control_count: int
    control_avg_reduction: float
    net_savings_per_lamp_per_day: float
    created_at: str
    stale_at: str | None = None
    stale_reasons: list[dict] = Field(default_factory=list)
    stale_segment_ids: list[int] = Field(default_factory=list)


class ReportSummary(BaseModel):
    id: int
    status: ReportStatus
    target_segment_id: int
    target_name: str
    target_retrofit_date: date
    comparison: ComparisonWindow
    control_count: int
    control_avg_reduction: float
    net_savings_per_lamp_per_day: float
    created_at: str
    stale_at: str | None = None


class ReportIssueResult(BaseModel):
    status: Literal["issued", "deferred"]
    target_segment_id: int
    comparison: ComparisonWindow | None = None
    report: ReportDetail | None = None
    reasons: list[dict] = Field(default_factory=list)


class AffectedReport(BaseModel):
    report_id: int
    previous_status: ReportStatus
    status: ReportStatus


class CorrectionResult(BaseModel):
    segment_id: int
    changed: bool
    changed_readings: list[date] = Field(default_factory=list)
    old_retrofit_date: date | None = None
    new_retrofit_date: date | None = None
    affected_reports: list[AffectedReport] = Field(default_factory=list)
    impact: dict


class HealthOut(BaseModel):
    status: str
    database: str
    version: str
