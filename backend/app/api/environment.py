from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.agents.llm import llm_enabled
from app.github_pr import github_token
from app.memory import memory_enabled

router = APIRouter()

DEMO_DIR = Path(__file__).resolve().parents[3] / "demo_target"


class Environment(BaseModel):
    demo_path: str | None = Field(description="The bundled demo app, when it ships next to the backend.")
    ai: bool = Field(description="Claude can explain root causes and write patches (credentials set, CODELOOP_LLM not off).")
    github: bool = Field(description="GITHUB_TOKEN is set, so pull requests can be created.")
    memory: bool = Field(description="Incidents are remembered between runs.")


@router.get("/environment", response_model=Environment)
async def get_environment() -> Environment:
    """What this server can do right now, so the UI can say what is switched on before a run starts."""
    credentials = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return Environment(
        demo_path=str(DEMO_DIR) if DEMO_DIR.is_dir() else None,
        ai=llm_enabled() and credentials,
        github=github_token() is not None,
        memory=memory_enabled(),
    )
