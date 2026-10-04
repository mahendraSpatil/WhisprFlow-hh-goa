from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response, status

from app import github_pr
from app.github_pr import PullRequestError, PullRequestPlan
from app.models.pr import PullRequestBlocker, PullRequestRequest, PullRequestResult, PullRequestStatus
from app.orchestrator.registry import RunHandle

router = APIRouter()


def _handle(request: Request, run_id: str) -> RunHandle:
    handle = request.app.state.runs.get(run_id)
    if handle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return handle


def _fail(exc: PullRequestError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message})


def _plan(handle: RunHandle) -> PullRequestPlan:
    """The pull request for this run. Raises PullRequestError if the run is not ready or the repo is unknown."""
    ctx = handle.context
    github_pr.select_patches(ctx)  # regression_not_ready / no_accepted_patches before anything else
    repo = github_pr.detect_repo(ctx.source_kind, ctx.fetch_target)
    if repo is None:
        raise PullRequestError("github_repo_unknown", github_pr.REPO_UNKNOWN, 400)
    return github_pr.plan_from_run(ctx, repo)


@router.get("/runs/{run_id}/pull-request", response_model=PullRequestStatus)
async def get_pull_request_status(run_id: str, request: Request) -> PullRequestStatus:
    """Whether a pull request can be created now, and if not, why (``blocker``). Includes the pull request once created.

    409 until RegressionCheck has finished with at least one accepted patch.
    """
    handle = _handle(request, run_id)
    branch = f"{github_pr.BRANCH_PREFIX}{run_id}"
    try:
        plan = _plan(handle)
    except PullRequestError as exc:
        if exc.status == 409:
            raise _fail(exc)
        return PullRequestStatus(ready=False, blocker=PullRequestBlocker(code=exc.code, message=exc.message), branch=branch, pull_request=handle.pull_request)
    blocker = None
    if github_pr.github_token() is None:
        blocker = PullRequestBlocker(code="github_token_missing", message=github_pr.TOKEN_HELP)
    return PullRequestStatus(
        ready=blocker is None,
        blocker=blocker,
        repo=plan.repo,
        branch=plan.branch,
        base=plan.base_branch,
        title=plan.title,
        patch_ids=[p.patch.id for p in plan.patches],
        pull_request=handle.pull_request,
    )


@router.post("/runs/{run_id}/pull-request", response_model=PullRequestResult, status_code=status.HTTP_201_CREATED)
async def create_pull_request(run_id: str, request: Request, response: Response, body: PullRequestRequest | None = None) -> PullRequestResult:
    """Create the branch ``codeloop/fix-{run_id}``, commit the accepted patches (one commit each) and open the pull request.

    Idempotent: a second call returns the same pull request with ``created`` false (HTTP 200).
    Errors carry ``detail = {code, message}``: github_token_missing, github_repo_unknown, regression_not_ready,
    no_accepted_patches, branch_exists, patches_not_applicable, and the github_* failures.
    """
    handle = _handle(request, run_id)
    async with handle.pr_lock:
        if handle.pull_request is not None:
            response.status_code = status.HTTP_200_OK
            return handle.pull_request.model_copy(update={"created": False, "notes": ["A pull request for this run already existed, so nothing was created."]})
        try:
            plan = _plan(handle)
            token = github_pr.github_token()
            if token is None:
                raise PullRequestError("github_token_missing", github_pr.TOKEN_HELP, 400)
            draft = body.draft if body else False

            def work() -> PullRequestResult:
                return github_pr.open_pull_request(request.app.state.github_factory(token), plan, draft=draft, token=token)

            result = await asyncio.to_thread(work)
        except PullRequestError as exc:
            raise _fail(exc)
        handle.pull_request = result
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result
