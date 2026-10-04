"""Models for opening a pull request from a run's accepted patches."""

from __future__ import annotations

from pydantic import Field

from app.models.agents import Contract


class PullRequestRequest(Contract):
    draft: bool = Field(default=False, description="Open the pull request as a draft.")


class PullRequestResult(Contract):
    number: int
    url: str
    repo: str = Field(description="owner/name")
    branch: str
    base: str
    title: str
    commits: int = Field(description="Commits CodeLoop made on the branch: one per accepted patch.")
    draft: bool
    created: bool = Field(description="False when an open pull request for this branch already existed.")
    notes: list[str] = Field(default=[], description="Things worth knowing, e.g. the base commit was not on GitHub.")


class PullRequestBlocker(Contract):
    code: str
    message: str


class PullRequestStatus(Contract):
    """What the UI needs before offering the button, and the pull request once it exists."""

    ready: bool
    blocker: PullRequestBlocker | None = None
    repo: str | None = None
    branch: str
    base: str | None = None
    title: str = ""
    patch_ids: list[str] = []
    pull_request: PullRequestResult | None = None
