from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient
from git import Repo

from app import github_pr
from app.github_pr import PullRequestError
from app.main import create_app
from app.models.agents import (
    ChainRole,
    ChainStep,
    DiagnosticSentinelOutput,
    ExplanationSource,
    Finding,
    FindingCategory,
    FindingRef,
    FindingSource,
    Patch,
    PatchedFile,
    PatchMasterOutput,
    PatchStatus,
    PatchVerdict,
    RegressionCheckOutput,
    RegressionSummary,
    RepoScoutOutput,
    RootCause,
    RootCauseDiagnosticianOutput,
    Severity,
    SourceKind,
    SourceLocation,
    SuiteTotals,
    Verdict,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import RunEventStream
from app.orchestrator.runner import Orchestrator

from fake_github import TOKEN, FakeGitHub
from helpers import make_diff

REPO = "acme/shop"
RUN_ID = "a1b2c3d4e5f6"
GIT_URL = "https://github.com/acme/shop.git"

DB_OLD = "def find(conn, c):\n    return conn.execute(\"SELECT * FROM t WHERE c = '%s'\" % c)\n"
DB_NEW = "def find(conn, c):\n    return conn.execute(\"SELECT * FROM t WHERE c = ?\", (c,))\n"
SVC_OLD = "def total(q, p):\n    return p / q\n"
SVC_NEW = "def total(q, p):\n    if q == 0:\n        return 0\n    return p / q\n"
SVC_NEWER = "def total(q, p):\n    if q <= 0:\n        return 0\n    return p / q\n"
README = "# shop\n"

HOSTILE = "Use <img src=x onerror=alert(1)> thanks @octocat, see #12 and [click](http://evil.example)."


class Fix:
    def __init__(self, path, old, new, title, rule="B608", owasp="A03:2021", owasp_name="Injection", severity=Severity.HIGH, explanation=None):
        self.path, self.old, self.new, self.title, self.rule = path, old, new, title, rule
        self.owasp, self.owasp_name, self.severity = owasp, owasp_name, severity
        self.explanation = explanation or f"{title} because the input reaches the query unchecked."


FIXES = [
    Fix("shop/db.py", DB_OLD, DB_NEW, "Possible SQL injection vector"),
    Fix("shop/service.py", SVC_OLD, SVC_NEW, "ZeroDivisionError", rule="ZeroDivisionError", owasp=None, owasp_name=None, severity=Severity.MEDIUM),
]


def make_ctx(tmp_path, fixes=None, *, commit=None, branch="main", target=GIT_URL, kind=SourceKind.GIT, rejected=False, regression=True, accepted=True) -> RunContext:
    fixes = FIXES if fixes is None else fixes
    ctx = RunContext(run_id=RUN_ID, source=target, source_kind=kind, work_dir=tmp_path)
    ctx._fetch_target = target
    ctx.set_output(StageName.REPO_SCOUT, RepoScoutOutput(root=tmp_path, source_kind=kind, commit=commit, branch=branch))

    findings, causes, patches, verdicts, refs = [], [], [], [], []
    for n, fix in enumerate(fixes, 1):
        loc = SourceLocation(file=fix.path, line=2)
        finding = Finding(
            id=f"f-{n}", category=FindingCategory.SECURITY, source=FindingSource.BANDIT, rule_id=fix.rule, title=fix.title,
            severity=fix.severity, location=loc, evidence="x", owasp=fix.owasp, owasp_name=fix.owasp_name, seen_before=1 if n == 1 else 0,
        )
        findings.append(finding)
        causes.append(RootCause(
            id=f"rc-{n}", finding_ids=[finding.id], location=loc, symbol=f"shop.mod{n}.func{n}", confidence=0.9,
            explanation=fix.explanation, explanation_source=ExplanationSource.TEMPLATE,
            chain=[
                ChainStep(node_id="main", label="api.handler", file="shop/api.py", line=5, role=ChainRole.ENTRY),
                ChainStep(node_id="off", label=f"func{n}", file=fix.path, line=2, role=ChainRole.OFFENDER),
            ],
        ))
        patches.append(Patch(
            id=f"p-{n}", root_cause_id=f"rc-{n}", status=PatchStatus.VALID, diff=make_diff(fix.path, fix.old, fix.new),
            files=[PatchedFile(path=fix.path, original=fix.old, patched=fix.new)],
            design_suggestion=f"Design {n}: use a parameterized API so the whole class of bug goes away.",
        ))
        ref = FindingRef(finding_id=finding.id, rule_id=fix.rule, title=fix.title, severity=fix.severity, location=loc)
        refs.append(ref)
        verdicts.append(PatchVerdict(
            patch_id=f"p-{n}", root_cause_id=f"rc-{n}", verdict=Verdict.PASS, accepted=accepted,
            newly_passing=[f"tests/test_{n}.py::test_ok"], findings_resolved=[ref],
        ))
    if rejected:
        verdicts.append(PatchVerdict(patch_id="p-rej", verdict=Verdict.REGRESSED, accepted=False, reasons=["breaks 2 tests " + HOSTILE]))
    ctx.set_output(StageName.DIAGNOSTIC_SENTINEL, DiagnosticSentinelOutput(findings=findings))
    ctx.set_output(StageName.ROOT_CAUSE_DIAGNOSTICIAN, RootCauseDiagnosticianOutput(root_causes=causes))
    ctx.set_output(StageName.PATCH_MASTER, PatchMasterOutput(patches=patches))
    if regression:
        applied = [p.id for p in patches] if accepted else []
        ctx.set_output(StageName.REGRESSION_CHECK, RegressionCheckOutput(
            verdicts=verdicts,
            summary=RegressionSummary(
                patches_applied=applied, tests_before=SuiteTotals(passed=5, failed=3), tests_after=SuiteTotals(passed=8),
                tests_fixed=[t for v in verdicts for t in v.newly_passing], findings_before=3, findings_after=1, findings_resolved=refs,
            ),
        ))
    return ctx


@pytest.fixture
def gh(monkeypatch):
    server = FakeGitHub().start()
    monkeypatch.setenv("GITHUB_API_URL", server.url)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    yield server
    server.stop()


@pytest.fixture
def shop(gh):
    """acme/shop on the fake GitHub, at the commit the run analyzed."""
    repo = gh.add_repo(REPO)
    sha = repo.seed({"shop/db.py": DB_OLD, "shop/service.py": SVC_OLD, "README.md": README, "shop/run.sh": "#!/bin/sh\n"}, modes={"shop/db.py": "100755"})
    return repo, sha


def open_pr(ctx, draft=False, token=TOKEN):
    plan = github_pr.plan_from_run(ctx, REPO)
    return plan, github_pr.open_pull_request(github_pr.connect(token), plan, draft=draft, token=token)


# --- repo detection -------------------------------------------------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("https://github.com/acme/shop", "acme/shop"),
    ("https://github.com/acme/shop.git", "acme/shop"),
    ("https://github.com/acme/shop/", "acme/shop"),
    ("https://user:tok@github.com/acme/shop.git", "acme/shop"),
    ("git@github.com:acme/shop.git", "acme/shop"),
    ("ssh://git@github.com/acme/my.repo-2.git", "acme/my.repo-2"),
    ("https://gitlab.com/acme/shop.git", None),
    ("https://github.com/acme", None),
    ("https://github.com.evil.example/acme/shop", None),
    ("C:\\work\\shop", None),
])
def test_parse_github_repo(url, expected):
    assert github_pr.parse_github_repo(url) == expected


def test_detect_repo_from_url_origin_and_override(tmp_path, monkeypatch):
    assert github_pr.detect_repo(SourceKind.GIT, "https://tok@github.com/acme/shop.git") == "acme/shop"
    assert github_pr.detect_repo(SourceKind.GIT, "https://gitlab.com/acme/shop.git") is None

    local = tmp_path / "local"
    repo = Repo.init(local)
    assert github_pr.detect_repo(SourceKind.LOCAL, str(local)) is None  # no remote
    repo.create_remote("origin", "git@github.com:acme/local-shop.git")
    assert github_pr.detect_repo(SourceKind.LOCAL, str(local)) == "acme/local-shop"
    (local / "pkg").mkdir()
    assert github_pr.detect_repo(SourceKind.LOCAL, str(local / "pkg")) == "acme/local-shop"  # a subfolder of the repo
    assert github_pr.detect_repo(SourceKind.LOCAL, str(tmp_path / "missing")) is None

    monkeypatch.setenv("CODELOOP_GITHUB_REPO", "other/place")
    assert github_pr.detect_repo(SourceKind.LOCAL, str(tmp_path)) == "other/place"
    monkeypatch.setenv("CODELOOP_GITHUB_REPO", "not a repo")
    assert github_pr.detect_repo(SourceKind.GIT, GIT_URL) is None


# --- selection and body ---------------------------------------------------------------------------


def test_md_text_neutralizes_html_mentions_issue_links_and_link_targets():
    out = github_pr.md_text(HOSTILE)
    assert "<img" not in out and "&lt;img" in out
    assert "@octocat" not in out and "@\u200boctocat" in out
    assert "#12" not in out and "#\u200b12" in out
    assert "](http" not in out
    assert len(github_pr.md_text("x" * 5000, 100)) == 100


def test_select_patches_takes_accepted_ones_in_applied_order(tmp_path):
    ctx = make_ctx(tmp_path, rejected=True)
    ctx.regression_check.summary.patches_applied.reverse()
    assert [p.patch.id for p in github_pr.select_patches(ctx)] == ["p-2", "p-1"]


def test_select_patches_errors(tmp_path):
    with pytest.raises(PullRequestError) as not_ready:
        github_pr.select_patches(make_ctx(tmp_path, regression=False))
    assert (not_ready.value.code, not_ready.value.status) == ("regression_not_ready", 409)
    with pytest.raises(PullRequestError) as none:
        github_pr.select_patches(make_ctx(tmp_path, accepted=False))
    assert (none.value.code, none.value.status) == ("no_accepted_patches", 409)


def test_body_has_root_cause_chain_before_after_and_design(tmp_path):
    ctx = make_ctx(tmp_path, [Fix("shop/db.py", DB_OLD, DB_NEW, "Possible SQL injection vector", explanation=HOSTILE)], rejected=True, commit="abcdef1234567")
    plan = github_pr.plan_from_run(ctx, REPO)
    body = plan.body
    assert "### Before and after" in body
    assert "| Tests passing | 5 | 8 |" in body and "| Tests failing | 3 | 0 |" in body and "| Findings | 3 | 1 |" in body
    assert "**Root cause**" in body
    assert "**Causal chain**" in body and "`api.handler` (`shop/api.py:5`) → **`func1`** (`shop/db.py:2`)" in body
    assert "**Hardened design**" in body and "Design 1: use a parameterized API" in body
    assert "OWASP A03:2021 Injection" in body and "seen in 1 earlier run" in body
    assert "tests/test_1.py::test_ok" in body  # tests fixed
    assert "### Not included" in body and "`p-rej`" in body
    assert RUN_ID in body and "abcdef1" in body
    assert "<img" not in body and "@octocat" not in body and "](http" not in body  # nothing hostile survives
    assert plan.title == "Fix: Possible SQL injection vector in mod1.func1"
    assert len(body) < github_pr.MAX_BODY_CHARS


def test_body_stays_under_the_github_limit_with_many_patches(tmp_path):
    many = [Fix("shop/db.py", DB_OLD, DB_NEW, f"Issue {i}", explanation="word " * 400) for i in range(60)]
    plan = github_pr.plan_from_run(make_ctx(tmp_path, many), REPO)
    assert len(plan.body) < github_pr.MAX_BODY_CHARS
    assert "more patch(es)" in plan.body
    assert plan.title == "CodeLoop: fix 60 issues in shop"


# --- against PyGithub and the fake server ---------------------------------------------------------


def test_opens_a_pull_request_with_one_commit_per_patch(tmp_path, gh, shop):
    repo, base = shop
    ctx = make_ctx(tmp_path, commit=base)
    plan, result = open_pr(ctx)

    assert (result.created, result.commits, result.base, result.branch) == (True, 2, "main", f"codeloop/fix-{RUN_ID}")
    assert result.url == f"https://github.com/{REPO}/pull/1" and result.number == 1 and result.draft is False

    head = repo.branches[f"codeloop/fix-{RUN_ID}"]
    second = repo.commits[head]
    first = repo.commits[second["parents"][0]]
    assert first["parents"] == [base]  # a straight line on top of the analyzed commit
    assert first["message"].startswith("Fix: Possible SQL injection vector in mod1.func1")
    assert second["message"].startswith("Fix: ZeroDivisionError in mod2.func2")
    assert f"CodeLoop run {RUN_ID}, patch p-2." in second["message"]

    after, before = repo.files_at(head), repo.files_at(base)
    assert after["shop/db.py"][1].decode() == DB_NEW and after["shop/service.py"][1].decode() == SVC_NEW
    assert after["shop/db.py"][0] == "100755"  # the executable bit survived the rewrite
    untouched = lambda files: {p: v for p, v in files.items() if p not in ("shop/db.py", "shop/service.py")}  # noqa: E731
    assert untouched(after) == untouched(before)
    # the first commit changes only its own file
    assert repo.files_at(second["parents"][0])["shop/service.py"][1].decode() == SVC_OLD

    pr = repo.pulls[0]
    assert pr["title"] == plan.title and pr["body"] == plan.body and pr["draft"] is False
    assert pr["head"]["ref"] == f"codeloop/fix-{RUN_ID}" and pr["base"]["ref"] == "main"


def test_a_draft_pull_request(tmp_path, gh, shop):
    repo, base = shop
    _, result = open_pr(make_ctx(tmp_path, commit=base), draft=True)
    assert result.draft is True and repo.pulls[0]["draft"] is True


def test_two_patches_on_one_file_apply_in_order(tmp_path, gh, shop):
    repo, base = shop
    fixes = [FIXES[1], Fix("shop/service.py", SVC_NEW, SVC_NEWER, "Negative quantity", rule="ValueError", owasp=None, owasp_name=None)]
    _, result = open_pr(make_ctx(tmp_path, fixes, commit=base))
    assert result.commits == 2
    assert repo.files_at(repo.branches[result.branch])["shop/service.py"][1].decode() == SVC_NEWER


def test_a_patch_already_on_github_is_skipped_not_duplicated(tmp_path, gh):
    repo = gh.add_repo(REPO)
    base = repo.seed({"shop/db.py": DB_NEW, "shop/service.py": SVC_OLD})  # the SQL fix is already merged
    _, result = open_pr(make_ctx(tmp_path, commit=base))
    assert result.commits == 1
    assert repo.files_at(repo.branches[result.branch])["shop/db.py"][1].decode() == DB_NEW


def test_nothing_to_commit_when_everything_is_already_fixed(tmp_path, gh):
    repo = gh.add_repo(REPO)
    base = repo.seed({"shop/db.py": DB_NEW, "shop/service.py": SVC_NEW})
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert exc.value.code == "nothing_to_commit"
    assert not gh.calls("POST", "git/refs")


def test_crlf_files_keep_their_line_endings(tmp_path, gh):
    repo = gh.add_repo(REPO)
    old, new = SVC_OLD.replace("\n", "\r\n"), SVC_NEW.replace("\n", "\r\n")
    base = repo.seed({"shop/service.py": old})
    _, result = open_pr(make_ctx(tmp_path, [Fix("shop/service.py", old, new, "ZeroDivisionError")], commit=base))
    assert repo.files_at(repo.branches[result.branch])["shop/service.py"][1] == new.encode()


def test_clicking_twice_returns_the_same_pull_request(tmp_path, gh, shop):
    repo, base = shop
    ctx = make_ctx(tmp_path, commit=base)
    _, first = open_pr(ctx)
    commits, pulls = len(repo.commits), len(repo.pulls)
    _, again = open_pr(ctx)
    assert (again.created, again.number, again.url) == (False, first.number, first.url)
    assert (len(repo.commits), len(repo.pulls)) == (commits, pulls)


def test_a_leftover_branch_without_a_pull_request_is_not_overwritten(tmp_path, gh, shop):
    repo, base = shop
    repo.branches[f"codeloop/fix-{RUN_ID}"] = base
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert (exc.value.code, exc.value.status) == ("branch_exists", 409)
    assert not gh.calls("POST", "git/")


def test_falls_back_to_the_branch_head_when_the_analyzed_commit_is_not_on_github(tmp_path, gh, shop):
    repo, base = shop
    _, result = open_pr(make_ctx(tmp_path, commit="f" * 40))
    assert result.commits == 2
    assert any("analyzed commit fffffff is not on GitHub" in n for n in result.notes)
    assert repo.commits[repo.commits[repo.branches[result.branch]]["parents"][0]]["parents"] == [base]


def test_targets_the_default_branch_when_the_analyzed_branch_is_not_on_github(tmp_path, gh, shop):
    _, result = open_pr(make_ctx(tmp_path, branch="feature/local", commit=None))
    assert result.base == "main" and any("feature/local" in n for n in result.notes)


def test_a_file_that_changed_on_github_stops_everything(tmp_path, gh, shop):
    repo, base = shop
    drifted = repo.seed({"shop/db.py": DB_OLD + "# changed upstream\n", "shop/service.py": SVC_OLD.replace("p / q", "p // q")}, branch="main")
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=drifted))
    assert (exc.value.code, exc.value.status) == ("patches_not_applicable", 409)
    # nothing visible was created: no branch, no pull request
    assert f"codeloop/fix-{RUN_ID}" not in repo.branches and not repo.pulls


def test_a_file_missing_on_github_is_reported(tmp_path, gh):
    repo = gh.add_repo(REPO)
    base = repo.seed({"README.md": README})
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert exc.value.code == "patches_not_applicable" and "does not exist on GitHub" in exc.value.message


# --- failures --------------------------------------------------------------------------------------


def test_a_bad_token(tmp_path, gh, shop):
    _, base = shop
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base), token="ghp_wrong")
    assert (exc.value.code, exc.value.status) == ("github_auth_failed", 502)
    assert "ghp_wrong" not in exc.value.message


def test_an_unknown_repo(tmp_path, gh):
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path))
    assert exc.value.code == "github_not_found" and REPO in exc.value.message


def test_a_token_without_write_access(tmp_path, gh, shop):
    _, base = shop
    gh.fail("POST", r"git/blobs$", 403, "Resource not accessible by personal access token")
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert exc.value.code == "github_forbidden" and "Contents and Pull requests" in exc.value.message
    assert f"codeloop/fix-{RUN_ID}" not in shop[0].branches


def test_rate_limit(tmp_path, gh, shop):
    _, base = shop
    gh.fail("GET", r"/contents/", 403, "API rate limit exceeded for user", headers={"X-RateLimit-Remaining": "0"})
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert exc.value.code == "github_rate_limited"


def test_github_refusing_the_pull_request_says_the_branch_exists(tmp_path, gh, shop):
    repo, base = shop
    gh.fail("POST", r"/pulls$", 422, "Validation Failed")
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert exc.value.code == "github_rejected" and f"codeloop/fix-{RUN_ID}" in exc.value.message and "2 commit(s)" in exc.value.message


def test_an_error_message_that_echoes_the_token_is_redacted(tmp_path, gh, shop):
    _, base = shop
    gh.fail("POST", r"git/blobs$", 422, f"bad credential {TOKEN}")
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path, commit=base))
    assert TOKEN not in exc.value.message and "***" in exc.value.message


def test_github_unreachable(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens here
    monkeypatch.setenv("GITHUB_API_URL", f"http://127.0.0.1:{port}")
    with pytest.raises(PullRequestError) as exc:
        open_pr(make_ctx(tmp_path))
    assert exc.value.code == "github_unreachable"


def test_the_token_only_travels_in_the_authorization_header(tmp_path, gh, shop):
    _, base = shop
    plan, _ = open_pr(make_ctx(tmp_path, commit=base))
    assert gh.requests and all(r.authorization in (f"token {TOKEN}", f"Bearer {TOKEN}") for r in gh.requests)
    assert all(TOKEN not in r.path and TOKEN not in str(r.body) for r in gh.requests)
    assert TOKEN not in plan.body


# --- the API ----------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    app = create_app(Orchestrator(workspace_root=tmp_path / "workspaces"))
    with TestClient(app) as client:
        client.app_state = app.state
        yield client


def add_run(api, ctx):
    api.app_state.runs.add(ctx, RunEventStream(ctx.run_id))
    return f"/runs/{ctx.run_id}/pull-request"


def error(response):
    return response.json()["detail"]


def test_status_and_post_without_a_token_are_clear_messages_not_crashes(api, tmp_path):
    url = add_run(api, make_ctx(tmp_path))
    status = api.get(url).json()
    assert status["ready"] is False and status["blocker"]["code"] == "github_token_missing"
    assert "GITHUB_TOKEN" in status["blocker"]["message"] and status["repo"] == REPO and status["branch"] == f"codeloop/fix-{RUN_ID}"
    assert status["patch_ids"] == ["p-1", "p-2"]

    response = api.post(url, json={})
    assert response.status_code == 400
    assert error(response)["code"] == "github_token_missing" and "GITHUB_TOKEN" in error(response)["message"]


def test_a_token_that_is_only_whitespace_counts_as_missing(api, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    assert error(api.post(add_run(api, make_ctx(tmp_path))))["code"] == "github_token_missing"


def test_status_is_409_until_the_regression_check_is_done(api, tmp_path):
    url = add_run(api, make_ctx(tmp_path, regression=False))
    assert api.get(url).status_code == 409 and error(api.get(url))["code"] == "regression_not_ready"
    assert api.post(url).status_code == 409


def test_status_reports_when_there_is_nothing_to_commit(api, tmp_path):
    url = add_run(api, make_ctx(tmp_path, accepted=False))
    assert error(api.get(url))["code"] == "no_accepted_patches"


def test_a_source_that_is_not_on_github(api, tmp_path):
    url = add_run(api, make_ctx(tmp_path, target="https://gitlab.com/acme/shop.git"))
    status = api.get(url).json()
    assert status["ready"] is False and status["blocker"]["code"] == "github_repo_unknown" and "CODELOOP_GITHUB_REPO" in status["blocker"]["message"]
    assert error(api.post(url))["code"] == "github_repo_unknown"


def test_unknown_run(api):
    assert api.get("/runs/nope/pull-request").status_code == 404
    assert api.post("/runs/nope/pull-request").status_code == 404


def test_create_pull_request_end_to_end(api, tmp_path, gh, shop):
    repo, base = shop
    url = add_run(api, make_ctx(tmp_path, commit=base))
    assert api.get(url).json()["ready"] is True

    created = api.post(url, json={"draft": True})
    assert created.status_code == 201
    body = created.json()
    assert body["url"] == f"https://github.com/{REPO}/pull/1" and body["commits"] == 2 and body["created"] is True and body["draft"] is True
    assert TOKEN not in created.text

    again = api.post(url)
    assert again.status_code == 200 and again.json()["created"] is False and again.json()["url"] == body["url"]
    assert len(repo.pulls) == 1

    status = api.get(url).json()
    assert status["pull_request"]["url"] == body["url"]


def test_github_errors_come_back_as_codes(api, tmp_path, gh, shop):
    _, base = shop
    url = add_run(api, make_ctx(tmp_path, commit=base))
    gh.fail("POST", r"/pulls$", 422, "Validation Failed")
    response = api.post(url)
    assert response.status_code == 502 and error(response)["code"] == "github_rejected"
    assert api.get(url).json()["pull_request"] is None  # nothing was recorded as created


def test_the_client_comes_from_the_app_factory(api, tmp_path, gh, shop):
    _, base = shop
    seen = []

    def factory(token):
        seen.append(token)
        return github_pr.connect(token)

    api.app_state.github_factory = factory
    assert api.post(add_run(api, make_ctx(tmp_path, commit=base))).status_code == 201
    assert seen == [TOKEN]
