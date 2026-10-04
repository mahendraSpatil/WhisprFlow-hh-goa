from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from app.graph_overlay import overlay_findings
from app.models.agents import (
    DiagnosticSentinelOutput,
    Finding,
    Graph,
    Patch,
    PatchMasterOutput,
    PipelineArchitectOutput,
    RegressionCheckOutput,
    RootCause,
    RootCauseDiagnosticianOutput,
    SourceKind,
    SourceReport,
)
from app.models.run import RunStatus, RunSummary, StageName
from app.orchestrator.registry import RunHandle, RunRegistry
from app.orchestrator.runner import Orchestrator
from app.sources import InvalidSource, classify_source

router = APIRouter()

WS_CLOSE_UNKNOWN_RUN = 4404


class CreateRunRequest(BaseModel):
    source: str = Field(
        min_length=1,
        max_length=2048,
        description="Git URL (https://, ssh:// or git@host:path) or a directory on the server.",
    )


class CreateRunResponse(BaseModel):
    run_id: str
    status: RunStatus
    source: str
    source_kind: SourceKind
    events_url: str


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=CreateRunResponse)
async def create_run(body: CreateRunRequest, request: Request) -> CreateRunResponse:
    try:
        kind, target = classify_source(body.source)
    except InvalidSource as exc:
        raise HTTPException(422, str(exc)) from exc

    orchestrator: Orchestrator = request.app.state.orchestrator
    runs: RunRegistry = request.app.state.runs
    ctx, events = orchestrator.create_run(kind, target)
    handle = runs.add(ctx, events)
    handle.task = asyncio.create_task(orchestrator.execute(ctx, events), name=f"run-{ctx.run_id}")
    return CreateRunResponse(
        run_id=ctx.run_id,
        status=ctx.status,
        source=ctx.source,
        source_kind=ctx.source_kind,
        events_url=f"/ws/runs/{ctx.run_id}",
    )


def _handle(request: Request, run_id: str) -> RunHandle:
    handle = request.app.state.runs.get(run_id)
    if handle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return handle


@router.get("/runs/{run_id}", response_model=RunSummary)
async def get_run(run_id: str, request: Request) -> RunSummary:
    return _handle(request, run_id).context.summary()


@router.get("/runs/{run_id}/graph", response_model=Graph)
async def get_run_graph(run_id: str, request: Request) -> Graph:
    """The data-flow graph, once PipelineArchitect has succeeded.

    409 while it is pending or running (retry after its ``stage.status`` success
    event), or if it escalated or was skipped; ``detail`` carries the stage status.
    """
    ctx = _handle(request, run_id).context
    architect = ctx.optional(StageName.PIPELINE_ARCHITECT, PipelineArchitectOutput)
    if architect is None:
        record = ctx.stages.get(StageName.PIPELINE_ARCHITECT)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "graph is not available",
                "stage_status": record.status if record else None,
                "skipped_reason": record.skipped_reason if record else None,
            },
        )
    sentinel = ctx.optional(StageName.DIAGNOSTIC_SENTINEL, DiagnosticSentinelOutput)
    if sentinel is None:
        return architect.graph
    regression = ctx.optional(StageName.REGRESSION_CHECK, RegressionCheckOutput)
    return overlay_findings(architect.graph, sentinel.findings, regression)


class Diagnostics(BaseModel):
    findings: list[Finding]
    sources: list[SourceReport]
    root_causes: list[RootCause]
    root_causes_ready: bool = Field(description="False while RootCauseDiagnostician has not finished (or failed).")


@router.get("/runs/{run_id}/diagnostics", response_model=Diagnostics)
async def get_run_diagnostics(run_id: str, request: Request) -> Diagnostics:
    """Findings from DiagnosticSentinel and, once available, their root causes with causal chains.

    409 until DiagnosticSentinel has succeeded; ``detail`` carries the stage status.
    """
    ctx = _handle(request, run_id).context
    sentinel = ctx.optional(StageName.DIAGNOSTIC_SENTINEL, DiagnosticSentinelOutput)
    if sentinel is None:
        record = ctx.stages.get(StageName.DIAGNOSTIC_SENTINEL)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "diagnostics are not available",
                "stage_status": record.status if record else None,
                "skipped_reason": record.skipped_reason if record else None,
            },
        )
    causes = ctx.optional(StageName.ROOT_CAUSE_DIAGNOSTICIAN, RootCauseDiagnosticianOutput)
    return Diagnostics(
        findings=sentinel.findings,
        sources=sentinel.sources,
        root_causes=causes.root_causes if causes else [],
        root_causes_ready=causes is not None,
    )


class Patches(BaseModel):
    patches: list[Patch]


@router.get("/runs/{run_id}/patches", response_model=Patches)
async def get_run_patches(run_id: str, request: Request) -> Patches:
    """Patches from PatchMaster, one per root cause: valid ones carry the diff and before/after files.

    409 until PatchMaster has succeeded; ``detail`` carries the stage status.
    """
    ctx = _handle(request, run_id).context
    output = ctx.optional(StageName.PATCH_MASTER, PatchMasterOutput)
    if output is None:
        record = ctx.stages.get(StageName.PATCH_MASTER)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "patches are not available",
                "stage_status": record.status if record else None,
                "skipped_reason": record.skipped_reason if record else None,
            },
        )
    return Patches(patches=output.patches)


@router.get("/runs/{run_id}/regression", response_model=RegressionCheckOutput)
async def get_run_regression(run_id: str, request: Request) -> RegressionCheckOutput:
    """RegressionCheck's verdict per patch, and the before/after summary for the accepted patches together.

    409 until RegressionCheck has succeeded; ``detail`` carries the stage status.
    """
    ctx = _handle(request, run_id).context
    output = ctx.optional(StageName.REGRESSION_CHECK, RegressionCheckOutput)
    if output is None:
        record = ctx.stages.get(StageName.REGRESSION_CHECK)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "the regression check is not available",
                "stage_status": record.status if record else None,
                "skipped_reason": record.skipped_reason if record else None,
            },
        )
    return output


@router.websocket("/ws/runs/{run_id}")
async def run_events(websocket: WebSocket, run_id: str, since: int = Query(0, ge=0)) -> None:
    """Replays the run's events after ``since``, then streams live ones until the run ends."""
    await websocket.accept()
    handle = websocket.app.state.runs.get(run_id)
    if handle is None:
        await websocket.close(code=WS_CLOSE_UNKNOWN_RUN, reason="run not found")
        return
    try:
        async for event in handle.events.subscribe(since):
            await websocket.send_text(event.model_dump_json())
        await websocket.close()
    except WebSocketDisconnect:
        pass
