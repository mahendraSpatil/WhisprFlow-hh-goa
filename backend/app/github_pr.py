"""Opening a pull request with a run's accepted patches, through PyGithub.

The sequence, all through the Git Data API so each patch is one atomic commit:

1. Work out the target repo (the run's GitHub URL, a local repo's ``origin``, or CODELOOP_GITHUB_REPO).
2. Fetch the files the patches touch *as they are on GitHub* at the analyzed commit, and apply the
   accepted patches to them in order. If a patch no longer applies, stop: the repo moved on, or the
   analyzed copy had local changes, and a PR built on a guess would be wrong.
3. Create one commit per patch (blobs, a tree on top of the previous one, a commit on top of the previous
   one), then the branch ``codeloop/fix-{run id}`` at the last commit, then the pull request. Nothing is
   visible on GitHub until the branch is created, so a failure midway leaves no half-built branch.

The token comes from GITHUB_TOKEN, is read when needed, and is never stored, logged or returned.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import requests
from git import InvalidGitRepositoryError, NoSuchPathError, Repo
from github import (
    Auth,
    BadCredentialsException,
    Github,
    GithubException,
    InputGitTreeElement,
    RateLimitExceededException,
    UnknownObjectException,
)

from app.agents.patching import apply_patch
from app.models.agents import (
    Finding,
    Patch,
    PatchStatus,
    PatchVerdict,
    RegressionCheckOutput,
    RegressionSummary,
    RootCause,
    Severity,
    SourceKind,
)
from app.models.pr import PullRequestResult
from app.models.run import RunContext, StageName

BRANCH_PREFIX = "codeloop/fix-"
MAX_BODY_CHARS = 60_000  # GitHub allows 65,536
MAX_SECTION_TEXT = 1800
MAX_CHAIN_STEPS = 14
MAX_SUBJECT_CHARS = 72
# GitHub asks clients to pause between mutating requests (secondary rate limits); PyGithub's defaults do.
SECONDS_BETWEEN_REQUESTS = 0.25
SECONDS_BETWEEN_WRITES = 1.0
TOKEN_HELP = (
    "GITHUB_TOKEN is not set on the CodeLoop server, so no pull request can be created. Create a token with "
    "write access to Contents and Pull requests for the target repository, set it as GITHUB_TOKEN, and "
    "restart the backend."
)
SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}


class PullRequestError(Exception):
    """A failure the user can act on. ``code`` is stable for the UI; ``status`` is the HTTP status to answer with."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def github_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN", "").strip() or None


def api_base_url() -> str:
    return os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com"


def connect(token: str) -> Github:
    return Github(
        auth=Auth.Token(token), base_url=api_base_url(), timeout=30, retry=2,
        seconds_between_requests=SECONDS_BETWEEN_REQUESTS, seconds_between_writes=SECONDS_BETWEEN_WRITES,
    )


def redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


# --- Which repo ----------------------------------------------------------------------------------

_GITHUB_URL = re.compile(
    r"^(?:https?://(?:[^@/\s]+@)?github\.com/|ssh://(?:[^@/\s]+@)?github\.com/|git@github\.com:)"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)
_REPO_NAME = re.compile(r"^[\w.-]+/[\w.-]+$")


def parse_github_repo(url: str) -> str | None:
    """``owner/name`` for a github.com URL (https, ssh or scp-style), else None."""
    match = _GITHUB_URL.match(url.strip())
    return f"{match['owner']}/{match['name']}" if match else None


def _local_remote_url(path: str) -> str | None:
    try:
        repo = Repo(path, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None
    remotes = list(repo.remotes)
    origin = next((r for r in remotes if r.name == "origin"), remotes[0] if remotes else None)
    return origin.url if origin else None


def detect_repo(source_kind: SourceKind, fetch_target: str) -> str | None:
    """The GitHub repo to open the pull request in. CODELOOP_GITHUB_REPO=owner/name always wins."""
    override = os.environ.get("CODELOOP_GITHUB_REPO", "").strip()
    if override:
        return override if _REPO_NAME.match(override) else None
    url = fetch_target if source_kind is SourceKind.GIT else _local_remote_url(fetch_target)
    return parse_github_repo(url) if url else None


REPO_UNKNOWN = (
    "This run's source is not a GitHub repository (no github.com URL or origin remote), so there is nowhere to "
    "open the pull request. Set CODELOOP_GITHUB_REPO=owner/name to choose the target."
)


# --- The plan: what to commit and what to say -----------------------------------------------------


@dataclass
class PlannedPatch:
    patch: Patch
    cause: RootCause | None
    verdict: PatchVerdict
    findings: list[Finding]


@dataclass
class PullRequestPlan:
    repo: str
    branch: str
    base_branch: str | None
    base_sha: str | None
    title: str
    body: str
    patches: list[PlannedPatch]
    messages: list[str]  # one commit message per patch, in order

    @property
    def paths(self) -> list[str]:
        return list(dict.fromkeys(f.path for p in self.patches for f in p.patch.files))


def md_text(text: str, limit: int = MAX_SECTION_TEXT) -> str:
    """Text written by a model or taken from a repo, made safe to put in a PR body: no HTML, no pings, no
    issue links, no link targets. It still reads normally."""
    text = text.replace("\r", "").strip()
    text = text.replace("<", "&lt;").replace(">", "&gt;").replace("](", "] (")
    text = re.sub(r"@(?=\w)", "@​", text)
    text = re.sub(r"#(?=\d)", "#​", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def code(text: str) -> str:
    return "`" + str(text).replace("`", "'").replace("\n", " ") + "`"


def _short_symbol(symbol: str | None) -> str | None:
    return ".".join(symbol.split(".")[-2:]) if symbol else None


def select_patches(ctx: RunContext) -> list[PlannedPatch]:
    """The accepted patches that make up the final state, in the order RegressionCheck applied them."""
    regression = ctx.optional(StageName.REGRESSION_CHECK, RegressionCheckOutput)
    if regression is None:
        raise PullRequestError("regression_not_ready", "The regression check has not finished, so no patch is verified yet.", 409)
    patches = {p.id: p for p in (ctx.patch_master.patches if ctx.patch_master else [])}
    verdicts = {v.patch_id: v for v in regression.verdicts}
    causes = {c.id: c for c in (ctx.root_cause_diagnostician.root_causes if ctx.root_cause_diagnostician else [])}
    findings = ctx.diagnostic_sentinel.findings if ctx.diagnostic_sentinel else []
    applied = regression.summary.patches_applied if regression.summary else []

    planned = []
    for patch_id in applied:
        patch, verdict = patches.get(patch_id), verdicts.get(patch_id)
        if patch is None or verdict is None or not verdict.accepted or patch.status is not PatchStatus.VALID:
            continue
        cause = causes.get(patch.root_cause_id)
        own = [f for f in findings if cause and f.id in cause.finding_ids]
        planned.append(PlannedPatch(patch, cause, verdict, sorted(own, key=lambda f: SEVERITY_RANK[f.severity])))
    if not planned:
        raise PullRequestError("no_accepted_patches", "No patch passed the regression check, so there is nothing to commit.", 409)
    return planned


def _subject(planned: PlannedPatch) -> str:
    top = planned.findings[0].title.rstrip(".") if planned.findings else "Fix an issue"
    where = _short_symbol(planned.cause.symbol if planned.cause else None)
    text = f"Fix: {top}" + (f" in {where}" if where else "")
    text = " ".join(text.split())
    return text if len(text) <= MAX_SUBJECT_CHARS else text[: MAX_SUBJECT_CHARS - 1].rstrip() + "…"


def _commit_message(planned: PlannedPatch, run_id: str) -> str:
    v = planned.verdict
    lines = [_subject(planned), ""]
    if planned.cause:
        lines += [planned.cause.explanation.strip(), ""]
    lines.append(
        f"Verified by CodeLoop on a fresh copy of the repo: fixes {len(v.newly_passing)} tests, resolves "
        f"{len(v.findings_resolved)} findings, breaks nothing."
    )
    lines.append(f"CodeLoop run {run_id}, patch {planned.patch.id}.")
    return "\n".join(lines)


def _table(summary: RegressionSummary) -> str:
    def row(label: str, before: int, after: int) -> str:
        return f"| {label} | {before} | {after} |"

    failing = lambda t: t.failed + t.errors  # noqa: E731
    return "\n".join(
        [
            "| | Before | After |",
            "|---|---:|---:|",
            row("Tests passing", summary.tests_before.passed, summary.tests_after.passed),
            row("Tests failing", failing(summary.tests_before), failing(summary.tests_after)),
            row("Findings", summary.findings_before, summary.findings_after),
        ]
    )


def _chain(cause: RootCause | None) -> str:
    if not cause or not cause.chain:
        return "_No path from an entry point to this code was found in the call graph._"
    steps = cause.chain
    shown = steps if len(steps) <= MAX_CHAIN_STEPS else [*steps[:3], None, *steps[-(MAX_CHAIN_STEPS - 4) :]]
    parts = []
    for step in shown:
        if step is None:
            parts.append("…")
            continue
        where = f" ({code(f'{step.file}:{step.line}')})" if step.file and step.line else ""
        label = code(step.label)
        parts.append(f"**{label}**{where}" if step.role.value == "offender" else f"{label}{where}")
    return " → ".join(parts)


def _section(n: int, planned: PlannedPatch) -> str:
    cause, v = planned.cause, planned.verdict
    top = planned.findings[0] if planned.findings else None
    heading = f"### {n}. {md_text(top.title.rstrip('.'), 140) if top else planned.patch.id}"
    meta = []
    if cause:
        meta.append(code(f"{cause.location.file}:{cause.location.line}"))
    if top:
        meta.append(f"{top.severity.value}" + (f" · OWASP {top.owasp} {md_text(top.owasp_name or '', 60)}" if top.owasp else ""))
        if top.seen_before:
            meta.append(f"seen in {top.seen_before} earlier run{'s' if top.seen_before != 1 else ''}")
    out = [heading, " · ".join(meta), ""]
    out += ["**Root cause**", "", md_text(cause.explanation) if cause else "_No root cause was recorded._", ""]
    out += ["**Causal chain**", "", _chain(cause), ""]
    design = planned.patch.design_suggestion.strip()
    out += ["**Hardened design**", "", md_text(design, 1400) if design else "_No design suggestion was recorded._", ""]
    resolved = ", ".join(code(f"{r.rule_id}") for r in v.findings_resolved[:6]) or "none"
    out.append(
        f"**Verification:** fixes {len(v.newly_passing)} test{'s' if len(v.newly_passing) != 1 else ''}, resolves "
        f"{len(v.findings_resolved)} finding{'s' if len(v.findings_resolved) != 1 else ''} ({resolved}), breaks nothing."
    )
    if planned.patch.memory_examples:
        n_examples = planned.patch.memory_examples
        out.append(f"\nThis fix was informed by {n_examples} accepted fix{'es' if n_examples != 1 else ''} for similar incidents in earlier runs.")
    return "\n".join(line for line in out if line is not None)


def render_body(ctx: RunContext, repo: str, planned: list[PlannedPatch], regression: RegressionCheckOutput) -> str:
    summary = regression.summary
    name = repo.split("/")[-1]
    head = [
        f"## CodeLoop: automated fixes for {code(name)}",
        "",
        f"CodeLoop analyzed this repository, found problems, and prepared {len(planned)} patch{'es' if len(planned) != 1 else ''}. "
        "Each patch was applied to a fresh copy of the repository, and the tests and the security scan were re-run "
        "before this pull request was opened. There is one commit per patch.",
        "",
        "> The patches were drafted by an AI model and checked automatically. Please review them like any other change.",
        "",
    ]
    if summary:
        head += ["### Before and after", "", _table(summary), ""]
        broken = f"{len(summary.tests_broken)} newly broken" if summary.tests_broken else "none newly broken"
        head.append(f"{len(summary.tests_fixed)} tests fixed, {broken}; {len(summary.findings_resolved)} findings resolved.")
        if summary.tests_fixed:
            names = "\n".join(f"- {code(t)}" for t in summary.tests_fixed[:40])
            more = f"\n- … and {len(summary.tests_fixed) - 40} more" if len(summary.tests_fixed) > 40 else ""
            head += ["", f"<details><summary>Tests fixed ({len(summary.tests_fixed)})</summary>", "", names + more, "", "</details>"]
        head.append("")

    tail = []
    rejected = [v for v in regression.verdicts if not v.accepted]
    if rejected:
        tail += ["---", "", "### Not included", "", "These patches were generated but rejected by the regression check:", ""]
        tail += [f"- {code(v.patch_id)}: {md_text('; '.join(v.reasons) or v.verdict.value, 300)}" for v in rejected]
        tail.append("")
    repo_scout = ctx.repo_scout
    analyzed = f" · analyzed commit {code(repo_scout.commit[:7])}" if repo_scout and repo_scout.commit else ""
    tail += ["---", "", f"_Generated by CodeLoop · run {code(ctx.run_id)}{analyzed}_"]

    budget = MAX_BODY_CHARS - len("\n".join(head)) - len("\n".join(tail)) - 200
    sections, used = [], 0
    for n, p in enumerate(planned, 1):
        section = _section(n, p)
        if used + len(section) > budget:
            sections.append(f"_… and {len(planned) - n + 1} more patch(es); see the commits on this branch._")
            break
        sections.append("---\n\n" + section)
        used += len(section)
    return "\n".join(head) + "\n" + "\n\n".join(sections) + "\n\n" + "\n".join(tail) + "\n"


def plan_from_run(ctx: RunContext, repo: str) -> PullRequestPlan:
    planned = select_patches(ctx)
    regression = ctx.optional(StageName.REGRESSION_CHECK, RegressionCheckOutput)
    assert regression is not None  # select_patches raised otherwise
    messages = [_commit_message(p, ctx.run_id) for p in planned]
    title = messages[0].splitlines()[0] if len(planned) == 1 else f"CodeLoop: fix {len(planned)} issues in {repo.split('/')[-1]}"
    scout = ctx.repo_scout
    return PullRequestPlan(
        repo=repo,
        branch=f"{BRANCH_PREFIX}{ctx.run_id}",
        base_branch=scout.branch if scout else None,
        base_sha=scout.commit if scout else None,
        title=title,
        body=render_body(ctx, repo, planned, regression),
        patches=planned,
        messages=messages,
    )


# --- Talking to GitHub ----------------------------------------------------------------------------


def _resolve_base(repo, plan: PullRequestPlan, notes: list[str]) -> tuple[str, str]:
    """(base branch, commit to build on). Prefers the analyzed commit, falls back to the branch head."""
    wanted = plan.base_branch or repo.default_branch
    branch = wanted
    try:
        head = repo.get_branch(branch).commit.sha
    except UnknownObjectException:
        if branch == repo.default_branch:
            raise PullRequestError("base_branch_missing", f"The branch {branch} does not exist on {plan.repo}.", 400)
        notes.append(f"The analyzed branch {branch} is not on GitHub, so the pull request targets {repo.default_branch}.")
        branch = repo.default_branch
        head = repo.get_branch(branch).commit.sha
    if plan.base_sha:
        try:
            repo.get_git_commit(plan.base_sha)
            return branch, plan.base_sha
        except UnknownObjectException:
            notes.append(
                f"The analyzed commit {plan.base_sha[:7]} is not on GitHub, so the fixes are applied on top of the "
                f"current head of {branch} ({head[:7]})."
            )
    return branch, head


def _fetch_files(repo, paths: list[str], sha: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts or ".." in parts or PurePosixPath(path).is_absolute():
            raise PullRequestError("patches_not_applicable", f"Refusing the path {path!r}.", 409)
        try:
            content = repo.get_contents(path, ref=sha)
        except UnknownObjectException:
            raise PullRequestError(
                "patches_not_applicable",
                f"{path} does not exist on GitHub at {sha[:7]}. The repository changed after the analysis; run it again.",
                409,
            )
        if isinstance(content, list) or content.type != "file" or content.encoding != "base64":
            raise PullRequestError("patches_not_applicable", f"{path} cannot be read through the GitHub contents API (is it over 1 MB?).", 409)
        files[path] = content.decoded_content
    return files


def _apply_in_order(patches: list[PlannedPatch], remote: dict[str, bytes], sha: str) -> list[dict[str, bytes]]:
    """For each patch, the files it changes (path -> new bytes), applying the patches in turn to GitHub's files."""
    states: list[dict[str, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="codeloop-pr-") as tmp:
        root = Path(tmp)
        for path, data in remote.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(data)
        current = dict(remote)
        for planned in patches:
            paths = [f.path for f in planned.patch.files]
            status, message = apply_patch(root, planned.patch.diff, paths)
            if status == "failed":
                detail = (message.strip().splitlines() or ["git apply failed"])[0]
                raise PullRequestError(
                    "patches_not_applicable",
                    f"Patch {planned.patch.id} does not apply to {', '.join(paths)} as it is on GitHub at {sha[:7]} ({detail}). "
                    "The file changed after the analysis, or the analyzed copy had local changes; run the analysis again.",
                    409,
                )
            changed: dict[str, bytes] = {}
            if status == "applied":
                for path in paths:
                    updated = (root / path).read_bytes()
                    if updated != current[path]:
                        changed[path], current[path] = updated, updated
            states.append(changed)
    return states


def _file_mode(repo, root_tree_sha: str, path: str) -> str:
    """The mode of an existing file (so patching a script does not drop its executable bit)."""
    sha, parts = root_tree_sha, path.split("/")
    for i, part in enumerate(parts):
        entry = next((e for e in repo.get_git_tree(sha).tree if e.path == part), None)
        if entry is None:
            return "100644"
        if i == len(parts) - 1:
            return entry.mode
        sha = entry.sha
    return "100644"


def open_pull_request(gh: Github, plan: PullRequestPlan, draft: bool = False, token: str | None = None) -> PullRequestResult:
    """Create the branch, the commits and the pull request; or return the pull request that already exists."""
    try:
        return _open(gh, plan, draft)
    except PullRequestError:
        raise
    except BadCredentialsException:
        raise PullRequestError("github_auth_failed", "GitHub rejected GITHUB_TOKEN (401). Check that it is valid and has not expired.", 502)
    except RateLimitExceededException:
        raise PullRequestError("github_rate_limited", "GitHub's API rate limit was reached. Try again later.", 502)
    except UnknownObjectException:
        raise PullRequestError(
            "github_not_found",
            f"GitHub could not find {plan.repo}, or GITHUB_TOKEN cannot see it. Check the repository name and the token's access.",
            502,
        )
    except GithubException as exc:
        message = redact(str((exc.data or {}).get("message", "")) if isinstance(exc.data, dict) else "", token)
        if exc.status == 403:
            raise PullRequestError(
                "github_forbidden",
                f"GitHub refused the request (403){': ' + message if message else ''}. The token needs write access to Contents and Pull requests on {plan.repo}.",
                502,
            )
        raise PullRequestError("github_rejected", f"GitHub rejected the request ({exc.status}){': ' + message if message else ''}.", 502)
    except requests.RequestException as exc:
        raise PullRequestError("github_unreachable", f"Could not reach GitHub ({type(exc).__name__}). Check the network and GITHUB_API_URL.", 502)


def _open(gh: Github, plan: PullRequestPlan, draft: bool) -> PullRequestResult:
    repo = gh.get_repo(plan.repo)
    owner = plan.repo.split("/")[0]
    notes: list[str] = []

    for existing in repo.get_pulls(state="open", head=f"{owner}:{plan.branch}"):
        return PullRequestResult(
            number=existing.number, url=existing.html_url, repo=plan.repo, branch=plan.branch, base=existing.base.ref,
            title=existing.title, commits=0, draft=bool(existing.draft), created=False,
            notes=["A pull request for this run already existed, so nothing was created."],
        )
    try:
        repo.get_branch(plan.branch)  # a real request (get_git_ref is lazy and never fails)
    except UnknownObjectException:
        pass
    else:
        raise PullRequestError(
            "branch_exists",
            f"The branch {plan.branch} already exists on {plan.repo} without an open pull request. Delete it on GitHub, or open the pull request from it by hand.",
            409,
        )

    base_branch, base_sha = _resolve_base(repo, plan, notes)
    remote = _fetch_files(repo, plan.paths, base_sha)
    states = _apply_in_order(plan.patches, remote, base_sha)
    if not any(states):
        raise PullRequestError("nothing_to_commit", f"The fixes are already present on {base_branch}; there is nothing to commit.", 409)

    parent = repo.get_git_commit(base_sha)
    tree = parent.tree
    modes = {path: _file_mode(repo, parent.tree.sha, path) for path in plan.paths}
    commits = 0
    for message, changed in zip(plan.messages, states):
        if not changed:
            continue
        elements = []
        for path, content in changed.items():
            blob = repo.create_git_blob(base64.b64encode(content).decode("ascii"), "base64")
            elements.append(InputGitTreeElement(path, modes[path], "blob", sha=blob.sha))
        tree = repo.create_git_tree(elements, tree)
        parent = repo.create_git_commit(message, tree, [parent])
        commits += 1

    repo.create_git_ref(f"refs/heads/{plan.branch}", parent.sha)
    try:
        pr = repo.create_pull(base=base_branch, head=plan.branch, title=plan.title, body=plan.body, draft=draft)
    except GithubException as exc:
        detail = str((exc.data or {}).get("message", "")) if isinstance(exc.data, dict) else ""
        raise PullRequestError(
            "github_rejected",
            f"The branch {plan.branch} was created with {commits} commit(s), but GitHub would not open the pull request"
            f" ({exc.status}{': ' + detail if detail else ''}).",
            502,
        )
    return PullRequestResult(
        number=pr.number, url=pr.html_url, repo=plan.repo, branch=plan.branch, base=base_branch, title=pr.title,
        commits=commits, draft=bool(pr.draft), created=True, notes=notes,
    )
