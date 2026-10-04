from __future__ import annotations

from app.agents.base import Agent
from app.agents.diagnostic_sentinel import DiagnosticSentinel
from app.agents.memory_keeper import MemoryKeeper
from app.agents.patch_master import PatchMaster
from app.agents.pipeline_architect import PipelineArchitect
from app.agents.regression_check import RegressionCheck
from app.agents.repo_scout import RepoScout
from app.agents.root_cause_diagnostician import RootCauseDiagnostician
from app.agents.sandbox_runner import SandboxRunner
from app.agents.system_analyst import SystemAnalyst

AGENT_TYPES: tuple[type[Agent], ...] = (
    RepoScout,
    SystemAnalyst,
    PipelineArchitect,
    SandboxRunner,
    DiagnosticSentinel,
    RootCauseDiagnostician,
    PatchMaster,
    RegressionCheck,
    MemoryKeeper,
)


def default_pipeline() -> list[Agent]:
    return [agent_type() for agent_type in AGENT_TYPES]


__all__ = ["AGENT_TYPES", "Agent", "default_pipeline"]
