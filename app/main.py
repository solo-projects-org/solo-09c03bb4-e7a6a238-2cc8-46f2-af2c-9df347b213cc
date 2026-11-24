"""FastAPI application factory and routes."""

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from . import __version__, service
from .config import Settings, load_settings
from .db import Database
from .models import (
    CorrectionResult,
    HealthOut,
    ReadingsOut,
    ReadingsUpsert,
    ReportDetail,
    ReportIssueResult,
    ReportSummary,
    SegmentCreate,
    SegmentOut,
    SegmentPatch,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db
        yield

    app = FastAPI(
        title=settings.app_title,
        version=settings.app_version,
        description=(
            "Per-segment street-light retrofit savings, with control-segment "
            "correction for concurrent electricity changes. Reports are "
            "immutable snapshots; corrections mark impacted reports as "
            "`pending_recalc` while keeping the original results readable."
        ),
        lifespan=lifespan,
    )

    def get_db() -> Database:
        return db

    DbDep = Annotated[Database, Depends(get_db)]

    @app.exception_handler(service.ApiError)
    async def _api_error_handler(_: Request, exc: service.ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, **exc.extra},
        )

    @app.get("/healthz", response_model=HealthOut, tags=["ops"])
    def healthz() -> HealthOut:
        with db.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return HealthOut(status="ok", database=settings.db_path, version=__version__)

    # -- segments ---------------------------------------------------------

    @app.post(
        "/api/segments",
        response_model=SegmentOut,
        status_code=201,
        tags=["segments"],
        summary="Create a road segment (retrofit_date omitted = not retrofitted)",
    )
    def create_segment(payload: SegmentCreate, database: DbDep) -> SegmentOut:
        return SegmentOut(**service.create_segment(database, payload.name, payload.retrofit_date))

    @app.get("/api/segments", response_model=list[SegmentOut], tags=["segments"])
    def list_segments(database: DbDep) -> list[SegmentOut]:
        return [SegmentOut(**s) for s in service.list_segments(database)]

    @app.get("/api/segments/{segment_id}", response_model=SegmentOut, tags=["segments"])
    def get_segment(segment_id: int, database: DbDep) -> SegmentOut:
        return SegmentOut(**service.get_segment(database, segment_id))

    @app.patch(
        "/api/segments/{segment_id}",
        response_model=CorrectionResult,
        tags=["corrections"],
        summary="Correct a retrofit date and mark impacted reports pending_recalc",
    )
    def patch_segment(
        segment_id: int, payload: SegmentPatch, database: DbDep
    ) -> CorrectionResult:
        return CorrectionResult(
            **service.correct_retrofit_date(database, segment_id, payload.retrofit_date)
        )

    @app.get(
        "/api/segments/{segment_id}/readings",
        response_model=ReadingsOut,
        tags=["readings"],
    )
    def get_readings(
        segment_id: int,
        database: DbDep,
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
    ) -> ReadingsOut:
        return ReadingsOut(
            segment_id=segment_id,
            readings=service.get_readings(database, segment_id, start, end),
        )

    @app.put(
        "/api/segments/{segment_id}/readings",
        response_model=CorrectionResult,
        tags=["corrections"],
        summary="Upsert daily readings and mark impacted reports pending_recalc",
    )
    def put_readings(
        segment_id: int, payload: ReadingsUpsert, database: DbDep
    ) -> CorrectionResult:
        values = [(r.date, r.kwh_per_lamp) for r in payload.readings]
        return CorrectionResult(
            **service.upsert_readings(database, segment_id, values)
        )

    @app.delete(
        "/api/segments/{segment_id}/readings/{reading_date}",
        response_model=CorrectionResult,
        tags=["corrections"],
        summary="Delete one daily reading and mark impacted reports pending_recalc",
    )
    def delete_reading(
        segment_id: int, reading_date: date, database: DbDep
    ) -> CorrectionResult:
        return CorrectionResult(
            **service.delete_reading(database, segment_id, reading_date)
        )

    # -- reports ----------------------------------------------------------

    @app.post(
        "/api/reports",
        response_model=ReportIssueResult,
        status_code=201,
        tags=["reports"],
        summary="Issue a savings report (201), or defer with reasons (200)",
        responses={200: {"description": "Deferred: no report was issued"}},
    )
    def issue_report(
        database: DbDep,
        response: Response,
        segment_id: Annotated[int, Query(ge=1)],
    ) -> ReportIssueResult:
        result = ReportIssueResult(**service.issue_report(database, segment_id))
        if result.status == "deferred":
            response.status_code = 200
        return result

    @app.get(
        "/api/reports",
        response_model=list[ReportSummary],
        tags=["reports"],
    )
    def list_reports(
        database: DbDep,
        target_segment_id: int | None = None,
        status: str | None = Query(default=None, pattern="^(issued|pending_recalc)$"),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[ReportSummary]:
        return [
            ReportSummary(**r)
            for r in service.list_reports(
                database,
                target_segment_id=target_segment_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        ]

    @app.get("/api/reports/{report_id}", response_model=ReportDetail, tags=["reports"])
    def get_report(report_id: int, database: DbDep) -> ReportDetail:
        return ReportDetail(**service.get_report(database, report_id))

    return app


app = create_app()
