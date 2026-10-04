from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import anthropic
import pytest
from helpers import FakeClient, Log, make_diff, reply, status_error, write_files

from app.agents import default_pipeline
from app.agents.patch_master import PatchMaster
from app.models.agents import (
    PatchMasterInput,
    PatchStatus,
    RepoScoutOutput,
    RootCause,
    SourceKind,
    SourceLocation,
)
from app.models.run import RunStatus, StageName
from app.orchestrator.runner import Orchestrator

pytestmark = pytest.mark.anyio

ORIGINAL = "def ratio(n):\n    return 100 // n\n\n\ndef other():\n    return 1\n"
FIXED = ORIGINAL.replace("return 100 // n", "return 100 // n if n else 0")
DESIGN = "Validate input at the boundary and make zero a first-class case, so callers cannot trigger the division."


def answer(diff: str, design: str = DESIGN) -> str:
    return f"<diff>\n{diff}</diff>\n\n<design>\n{design}\n</design>"


GOOD = answer(make_diff("app/calc.py", ORIGINAL, FIXED))
STALE = answer(make_diff("app/calc.py", ORIGINAL.replace("100 //", "100 /"), FIXED))  # context does not match


def cause(cid="RC-1", line=2, file="app/calc.py"):
    return RootCause(
        id=cid, finding_ids=["F-1"], location=SourceLocation(file=file, line=line), explanation="ratio divides by n without checking it.",
        confidence=0.9,
    )


@pytest.fixture
def repo(tmp_path):
    write_files(tmp_path, {"app/calc.py": ORIGINAL})
    return tmp_path


def run_agent(repo, client, causes=None):
    inp = PatchMasterInput(
        root=repo, repo=RepoScoutOutput(root=repo, source_kind=SourceKind.LOCAL), root_causes=[cause()] if causes is None else causes
    )
    log = Log()
    return PatchMaster(client=client).run(inp, log), log


async def patches(repo, client, causes=None):
    coroutine, log = run_agent(repo, client, causes)
    return (await coroutine).patches, log


# --- Attempts and the retry --------------------------------------------------------------


async def test_a_diff_that_applies_is_accepted_on_the_first_attempt(repo):
    client = FakeClient(reply(GOOD))
    (patch,), _ = await patches(repo, client)

    assert (patch.status, patch.attempts, patch.root_cause_id) == (PatchStatus.VALID, 1, "RC-1")
    assert patch.design_suggestion == DESIGN
    assert [(f.path, f.original, f.patched) for f in patch.files] == [("app/calc.py", ORIGINAL, FIXED)]
    assert patch.diff.startswith("--- a/app/calc.py\n+++ b/app/calc.py\n")
    assert (repo / "app" / "calc.py").read_text() == ORIGINAL  # the working copy is never modified

    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "medium"}
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    prompt = call["messages"][0]["content"]
    assert 'offending_line="2"' in prompt and "def ratio(n):\n    return 100 // n" in prompt  # the file, verbatim
    assert "ratio divides by n without checking it." in prompt  # the root cause
    assert "<diff>" in call["system"] and "<design>" in call["system"] and "untrusted" in call["system"]


async def test_a_rejected_diff_is_retried_once_with_gits_error(repo):
    first = reply(STALE)
    client = FakeClient(first, reply(GOOD))
    (patch,), log = await patches(repo, client)

    assert (patch.status, patch.attempts) == (PatchStatus.VALID, 2)
    assert patch.files[0].patched == FIXED
    assert any("rejected" in line and "retrying once" in line for line in log.lines)

    retry = client.calls[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert retry[1]["content"] is first.content  # the previous turn is echoed back unchanged
    assert "patch does not apply" in retry[2]["content"] and "ORIGINAL file" in retry[2]["content"]


async def test_two_rejected_diffs_leave_an_invalid_patch_with_the_last_error(repo):
    client = FakeClient(reply(STALE), reply(answer(make_diff("app/calc.py", "x\n", "y\n"), "A better design.")))
    (patch,), _ = await patches(repo, client)

    assert (patch.status, patch.attempts) == (PatchStatus.INVALID, 2)
    assert "patch does not apply" in patch.detail
    assert patch.design_suggestion == "A better design."  # the design survives a bad diff
    assert patch.files == [] and patch.diff.startswith("--- a/app/calc.py")
    assert len(client.calls) == 2  # exactly one retry


async def test_problems_other_than_git_failures_also_trigger_the_retry(repo):
    syntax = answer(make_diff("app/calc.py", ORIGINAL, ORIGINAL.replace("100 // n", "100 //")))
    outside = answer("--- a/../x.py\n+++ b/../x.py\n@@ -1 +1 @@\n-a\n+b\n")
    for bad, expected in ((syntax, "syntax error"), (outside, "not relative"), ("I could not find a fix.", "no <diff> block")):
        client = FakeClient(reply(bad), reply(GOOD))
        (patch,), _ = await patches(repo, client)
        assert (patch.status, patch.attempts) == (PatchStatus.VALID, 2)
        assert expected in client.calls[1]["messages"][2]["content"]


async def test_a_retry_that_gets_no_answer_keeps_the_first_error(repo):
    client = FakeClient(reply(STALE), anthropic.APIConnectionError(request=None))
    (patch,), _ = await patches(repo, client)
    assert patch.status is PatchStatus.INVALID and "patch does not apply" in patch.detail and "not answered" in patch.detail


# --- Claude unavailable ------------------------------------------------------------------


async def test_refusals_and_missing_credentials_skip_without_failing_the_stage(repo):
    (patch,), log = await patches(repo, FakeClient(reply(stop_reason="refusal", category="cyber")))
    assert patch.status is PatchStatus.SKIPPED and "did not return a patch" in patch.detail
    assert any("declined" in line and "cyber" in line for line in log.lines)

    failures = [
        TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set."),
        status_error(anthropic.AuthenticationError, 401),
    ]
    for failure in failures:
        client = FakeClient(failure)
        found, log = await patches(repo, client, [cause("RC-1"), cause("RC-2"), cause("RC-3")])
        assert [p.status for p in found] == [PatchStatus.SKIPPED] * 3 and "unavailable" in found[0].detail
        assert len(client.calls) == 1 and sum("disabled for this run" in line for line in log.lines) == 1  # warned once


async def test_the_off_switch_sends_nothing(repo, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the client must not be created when CODELOOP_LLM=off")

    monkeypatch.setattr("app.agents.patch_master.ClaudeClient", forbidden)
    coroutine, _ = run_agent(repo, None)
    (patch,) = (await coroutine).patches
    assert patch.status is PatchStatus.SKIPPED and "CODELOOP_LLM=off" in patch.detail


async def test_only_the_most_severe_root_causes_are_patched_and_unreadable_files_are_skipped(repo, monkeypatch):
    with monkeypatch.context() as limited:  # scoped: a bare monkeypatch.undo() would also undo the autouse isolation fixtures
        limited.setattr("app.agents.patch_master.MAX_PATCHES", 1)
        client = FakeClient(reply(GOOD))
        found, _ = await patches(repo, client, [cause("RC-1"), cause("RC-2")])
    assert [p.status for p in found] == [PatchStatus.VALID, PatchStatus.SKIPPED] and "most severe" in found[1].detail

    (missing,), _ = await patches(repo, FakeClient(reply(GOOD)), [cause(file="app/gone.py")])
    assert missing.status is PatchStatus.SKIPPED and "could not be read" in missing.detail
    (outside,), _ = await patches(repo, FakeClient(reply(GOOD)), [cause(file="../outside.py")])
    assert outside.status is PatchStatus.SKIPPED


async def test_the_client_is_closed_and_nothing_runs_without_root_causes(repo):
    client = FakeClient(reply(GOOD))
    await patches(repo, client)
    assert client.closed

    idle = FakeClient(reply(GOOD))
    empty, _ = await patches(repo, idle, [])
    assert empty == [] and idle.calls == []


# --- End to end on demo_target ---------------------------------------------------------------

DEMO_TARGET = Path(__file__).resolve().parents[2] / "demo_target"


def scripted_fixes(files: dict[str, str]):
    """A fake Claude that answers like a good engineer for demo_target's SQL injection and race."""

    def fix(kwargs):
        prompt = kwargs["messages"][0]["content"]
        if 'path="orders/db.py"' in prompt:
            old = files["orders/db.py"]
            new = old.replace(
                '''    query = f"SELECT id, customer, sku, quantity, total_cents FROM orders WHERE customer = '{customer}' ORDER BY id"
    return [_to_order(row) for row in conn.execute(query).fetchall()]''',
                '''    query = "SELECT id, customer, sku, quantity, total_cents FROM orders WHERE customer = ? ORDER BY id"
    return [_to_order(row) for row in conn.execute(query, (customer,)).fetchall()]''',
            )
            design = "Use parameterized queries everywhere and keep SQL text constant, so user input can never change a statement."
            return reply(answer(make_diff("orders/db.py", old, new), design))
        if "ZeroDivisionError" in prompt:
            old = files["orders/service.py"]
            new = old.replace(
                '"unit_price_cents": order.total_cents // order.quantity,',
                '"unit_price_cents": order.total_cents // order.quantity if order.quantity else 0,',
            )
            design = "Treat a zero quantity as a first-class state and compute derived prices in one place that handles it."
            return reply(answer(make_diff("orders/service.py", old, new), design))
        old = files["orders/service.py"]
        new = old.replace("import sqlite3\n", "import sqlite3\nimport threading\n", 1)
        new = new.replace("        self._warehouse = warehouse\n", "        self._warehouse = warehouse\n        self._lock = threading.Lock()\n", 1)
        new = new.replace(
            '''        current = self._stock.get(sku, 0)
        if quantity > current:
            raise OutOfStock(f"{sku}: requested {quantity}, only {current} left")
        self._warehouse.confirm_pick(sku, quantity)
        self._stock[sku] = current - quantity
        return self._stock[sku]''',
            '''        with self._lock:
            current = self._stock.get(sku, 0)
            if quantity > current:
                raise OutOfStock(f"{sku}: requested {quantity}, only {current} left")
            self._warehouse.confirm_pick(sku, quantity)
            self._stock[sku] = current - quantity
            return self._stock[sku]''',
        )
        design = "Guard all inventory mutations with one lock, or move stock into the database and decrement it atomically."
        return reply(answer(make_diff("orders/service.py", old, new), design))

    return fix


@pytest.mark.skipif(not DEMO_TARGET.is_dir(), reason="demo_target/ not present")
async def test_patches_for_demo_target_apply_and_fix_its_failing_tests(tmp_path):
    files = {rel: (DEMO_TARGET / rel).read_text() for rel in ("orders/db.py", "orders/service.py")}
    client = FakeClient(scripted_fixes(files))
    agents = [PatchMaster(client=client) if a.name is StageName.PATCH_MASTER else a for a in default_pipeline()]
    orchestrator = Orchestrator(workspace_root=tmp_path / "runs", agents=agents)
    ctx, events = orchestrator.create_run(SourceKind.LOCAL, str(DEMO_TARGET))
    await orchestrator.execute(ctx, events)
    assert ctx.status is RunStatus.COMPLETED

    patches = ctx.patch_master.patches
    by_status = {s: [p for p in patches if p.status is s] for s in PatchStatus}
    # four root causes sit in application code; the three assertion failures are in tests and are not patched
    assert len(by_status[PatchStatus.VALID]) == 4 and len(by_status[PatchStatus.SKIPPED]) == 3
    assert all("in a test" in p.detail or "surfaces in a test" in p.detail for p in by_status[PatchStatus.SKIPPED])
    found = {p.root_cause_id: p for p in by_status[PatchStatus.VALID]}
    assert {f.path for p in found.values() for f in p.files} == {"orders/db.py", "orders/service.py"}
    assert all(p.design_suggestion and p.attempts == 1 for p in found.values())

    # Apply the diffs in turn to a scratch copy (each patch was made against the original file, so they are
    # applied as diffs, not by overwriting files) and run demo_target's own tests there.
    from app.agents.patching import apply_patch

    scratch = tmp_path / "scratch"
    shutil.copytree(DEMO_TARGET, scratch, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".venv"))
    statuses = [apply_patch(scratch, p.diff, [f.path for f in p.files]) for p in found.values()]
    assert {s for s, _ in statuses} <= {"applied", "redundant"}, statuses  # the two SQL fixes are identical: one is redundant
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=scratch, capture_output=True, text=True
    )
    assert "24 passed" in result.stdout, result.stdout  # all three planted bugs are fixed
