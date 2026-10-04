from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import github_pr
from app.api.environment import router as environment_router
from app.api.memory import router as memory_router
from app.api.pulls import router as pulls_router
from app.api.runs import router as runs_router
from app.orchestrator.registry import RunRegistry
from app.orchestrator.runner import Orchestrator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def default_workspace_root() -> Path:
    configured = os.environ.get("CODELOOP_WORKSPACE_DIR")
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "codeloop" / "runs"


def create_app(orchestrator: Orchestrator | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.runs.shutdown()

    app = FastAPI(title="CodeLoop", version="0.1.0", lifespan=lifespan)
    app.state.orchestrator = orchestrator or Orchestrator(workspace_root=default_workspace_root())
    app.state.runs = RunRegistry()
    app.state.github_factory = github_pr.connect  # token -> PyGithub client; tests swap it
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CODELOOP_CORS_ORIGINS", "http://localhost:5173").split(","),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(runs_router)
    app.include_router(memory_router)
    app.include_router(environment_router)
    app.include_router(pulls_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
