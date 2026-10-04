from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from git import GitCommandError, Repo

from app.agents import default_pipeline
from app.agents.pipeline_architect import PipelineArchitect
from app.main import create_app
from app.models.events import run_event_adapter
from app.models.run import TERMINAL_RUN_STATUSES, RunStatus, StageName
from app.orchestrator.runner import Orchestrator


@pytest.fixture
def client(tmp_path):
    app = create_app(Orchestrator(workspace_root=tmp_path / "workspaces"))
    with TestClient(app) as client:
        yield client


def read_until_done(ws) -> list:
    events = []
    while True:
        event = run_event_adapter.validate_json(ws.receive_text())
        events.append(event)
        if event.type == "run.status" and event.status in TERMINAL_RUN_STATUSES:
            return events


def test_post_run_streams_to_completion(client, sample_repo):
    response = client.post("/runs", json={"source": str(sample_repo)})
    assert response.status_code == 202
    body = response.json()
    assert body["source_kind"] == "local"
    assert body["events_url"] == f"/ws/runs/{body['run_id']}"

    with client.websocket_connect(body["events_url"]) as ws:
        events = read_until_done(ws)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()  # server closes once the run ends

    assert events[0].seq == 1 and events[-1].status is RunStatus.COMPLETED
    assert {e.type for e in events} == {"run.status", "stage.status", "log"}

    summary = client.get(f"/runs/{body['run_id']}").json()
    assert summary["status"] == "completed"
    assert [s["status"] for s in summary["stages"]] == ["success"] * 9


def test_reconnect_with_since_skips_seen_events(client, sample_repo):
    run_id = client.post("/runs", json={"source": str(sample_repo)}).json()["run_id"]
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        total = len(read_until_done(ws))
    with client.websocket_connect(f"/ws/runs/{run_id}?since={total - 2}") as ws:
        replay = read_until_done(ws)
    assert [e.seq for e in replay] == [total - 1, total]


@pytest.mark.parametrize(
    "source, message",
    [
        ("http://example.com/repo.git", "https://"),
        ("ext::sh -c touch% /tmp/pwned", "Not a git URL"),
        ("--upload-pack=evil", "Not a git URL"),
        ("/definitely/not/a/dir", "Not a git URL"),
    ],
)
def test_post_run_rejects_bad_sources(client, source, message):
    response = client.post("/runs", json={"source": source})
    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_git_credentials_are_redacted(client, monkeypatch):
    url = "https://user:s3cret@github.com/org/repo.git"

    def failing_clone(clone_url, to_path, **kwargs):
        # What git really reports: the full URL appears in the command line and stderr.
        raise GitCommandError(["git", "clone", "--", clone_url, str(to_path)], 128, stderr=f"fatal: unable to access '{clone_url}'")

    monkeypatch.setattr(Repo, "clone_from", failing_clone)  # keep the test offline
    body = client.post("/runs", json={"source": url}).json()
    assert body["source"] == "https://***@github.com/org/repo.git"
    with client.websocket_connect(body["events_url"]) as ws:
        events = read_until_done(ws)

    assert events[-1].status is RunStatus.FAILED
    escalated = [e for e in events if e.type == "stage.status" and e.status == "escalated"]
    assert "unable to access" in escalated[0].error.message
    summary = client.get(f"/runs/{body['run_id']}").text
    for leaked in ("s3cret", "user:"):
        assert all(leaked not in e.model_dump_json() for e in events)
        assert leaked not in summary


def test_graph_endpoint(client, sample_repo):
    run_id = client.post("/runs", json={"source": str(sample_repo)}).json()["run_id"]
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        read_until_done(ws)

    response = client.get(f"/runs/{run_id}/graph")
    assert response.status_code == 200
    graph = response.json()
    assert {"nodes", "edges", "columns"} <= graph.keys()
    node = next(n for n in graph["nodes"] if n["id"] == "fn:sample.api.get_total")
    assert {"id", "label", "type", "file", "start_line", "end_line", "layer", "status", "metrics", "position"} <= node.keys()
    edge = graph["edges"][0]
    assert set(edge) == {"id", "source", "target", "kind", "animated"}

    assert client.get("/runs/nope/graph").status_code == 404


class BrokenArchitect(PipelineArchitect):
    async def run(self, inp, log):
        raise RuntimeError("layout exploded")


def test_graph_endpoint_when_architect_escalated(tmp_path, sample_repo):
    agents = [BrokenArchitect() if a.name is StageName.PIPELINE_ARCHITECT else a for a in default_pipeline()]
    app = create_app(Orchestrator(workspace_root=tmp_path / "workspaces", agents=agents))
    with TestClient(app) as client:
        run_id = client.post("/runs", json={"source": str(sample_repo)}).json()["run_id"]
        with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
            read_until_done(ws)
        response = client.get(f"/runs/{run_id}/graph")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "graph is not available",
        "stage_status": "escalated",
        "skipped_reason": None,
    }


def test_unknown_run_closes_websocket(client):
    with client.websocket_connect("/ws/runs/nope") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4404


RACY = {
    "app.py": (
        "import threading\n"
        "COUNTER = 0\n"
        "\n"
        "def bump():\n"
        "    global COUNTER\n"
        "    COUNTER += 1\n"
        "\n"
        "def main():\n"
        "    threading.Thread(target=bump).start()\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
}


def make_repo(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir()
    for name, content in files.items():
        (root / name).write_text(content)
    return root


def test_diagnostics_endpoint_and_graph_overlay(client, tmp_path):
    repo = make_repo(tmp_path, RACY)
    run_id = client.post("/runs", json={"source": str(repo)}).json()["run_id"]
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        read_until_done(ws)

    diagnostics = client.get(f"/runs/{run_id}/diagnostics")
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    (finding,) = body["findings"]
    assert (finding["rule_id"], finding["severity"], finding["node_id"]) == ("RACE001", "high", "fn:app.bump")
    assert body["root_causes_ready"] is True and body["root_causes"][0]["finding_ids"] == [finding["id"]]
    assert body["root_causes"][0]["chain"][-1]["role"] == "offender"
    assert {s["source"]: s["status"] for s in body["sources"]} == {"sandbox": "ok", "bandit": "ok", "static": "ok"}

    nodes = {n["id"]: n for n in client.get(f"/runs/{run_id}/graph").json()["nodes"]}
    assert nodes["fn:app.bump"]["status"] == "failing" and nodes["fn:app.bump"]["metrics"]["findings"] == 1
    assert nodes["fn:app.main"]["status"] == "idle" and nodes["fn:app.main"]["metrics"]["findings"] == 0

    assert client.get("/runs/nope/diagnostics").status_code == 404


def test_diagnostics_are_unavailable_when_the_sentinel_escalated(tmp_path, sample_repo):
    from app.agents.diagnostic_sentinel import DiagnosticSentinel

    class Broken(DiagnosticSentinel):
        async def run(self, inp, log):
            raise RuntimeError("scanner exploded")

    agents = [Broken() if a.name is StageName.DIAGNOSTIC_SENTINEL else a for a in default_pipeline()]
    app = create_app(Orchestrator(workspace_root=tmp_path / "workspaces", agents=agents))
    with TestClient(app) as client:
        run_id = client.post("/runs", json={"source": str(sample_repo)}).json()["run_id"]
        with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
            read_until_done(ws)
        response = client.get(f"/runs/{run_id}/diagnostics")
        assert client.get(f"/runs/{run_id}/graph").status_code == 200  # the graph does not depend on diagnosis

    assert response.status_code == 409 and response.json()["detail"]["stage_status"] == "escalated"


def test_patches_endpoint_reports_one_patch_per_root_cause(client, tmp_path):
    repo = make_repo(tmp_path, RACY)
    run_id = client.post("/runs", json={"source": str(repo)}).json()["run_id"]
    with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        read_until_done(ws)

    causes = client.get(f"/runs/{run_id}/diagnostics").json()["root_causes"]
    response = client.get(f"/runs/{run_id}/patches")
    assert response.status_code == 200
    patches = response.json()["patches"]
    # The tests run with CODELOOP_LLM=off, so nothing is requested and each patch says so.
    assert [(p["root_cause_id"], p["status"]) for p in patches] == [(c["id"], "skipped") for c in causes]
    assert "CODELOOP_LLM=off" in patches[0]["detail"] and patches[0]["files"] == []
    assert client.get("/runs/nope/patches").status_code == 404


def test_patches_are_unavailable_when_patch_master_escalated(tmp_path, sample_repo):
    from app.agents.patch_master import PatchMaster

    class Broken(PatchMaster):
        async def run(self, inp, log):
            raise RuntimeError("model exploded")

    agents = [Broken() if a.name is StageName.PATCH_MASTER else a for a in default_pipeline()]
    app = create_app(Orchestrator(workspace_root=tmp_path / "workspaces", agents=agents))
    with TestClient(app) as client:
        run_id = client.post("/runs", json={"source": str(sample_repo)}).json()["run_id"]
        with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
            read_until_done(ws)
        response = client.get(f"/runs/{run_id}/patches")

    assert response.status_code == 409 and response.json()["detail"]["stage_status"] == "escalated"


def test_memory_endpoint_lists_remembered_incidents(client, tmp_path, monkeypatch):
    assert client.get("/memory/incidents").json() == {"enabled": True, "total": 0, "incidents": []}

    repo = make_repo(tmp_path, RACY)
    for _ in range(2):  # the same repo twice: the second run recognizes the pattern
        run_id = client.post("/runs", json={"source": str(repo)}).json()["run_id"]
        with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
            read_until_done(ws)

    body = client.get("/memory/incidents").json()
    assert body["enabled"] is True and body["total"] == 2
    newest, oldest = body["incidents"]
    assert newest["run_id"] != oldest["run_id"] and newest["id"] > oldest["id"]  # newest first
    assert (newest["rule_id"], newest["kind"], newest["file"], newest["occurrences"]) == ("RACE001", "RACE001", "app.py", 2)
    assert newest["signature"] == oldest["signature"] and len(newest["signature"]) == 16
    assert newest["pattern"] == "_ += NUM" and newest["root_cause"] and newest["symbol"] == "app.bump"
    assert newest["patch_status"] == "skipped" and newest["regression_passed"] is None  # no patch: the LLM is off in tests
    assert newest["created_at"].endswith("+00:00")
    assert len(client.get("/memory/incidents?limit=1").json()["incidents"]) == 1

    # the second run's finding was rated higher, and its graph node says so
    findings = client.get(f"/runs/{run_id}/diagnostics").json()["findings"]
    assert findings[0]["seen_before"] == 1 and findings[0]["base_severity"] == "high" and findings[0]["severity"] == "critical"
    node = next(n for n in client.get(f"/runs/{run_id}/graph").json()["nodes"] if n["id"] == "fn:app.bump")
    assert node["metrics"]["seen_before"] == 1 and node["status"] == "failing"

    monkeypatch.setenv("CODELOOP_MEMORY", "off")
    assert client.get("/memory/incidents").json() == {"enabled": False, "total": 0, "incidents": []}


def test_environment_reports_what_is_switched_on(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    off = client.get("/environment").json()
    assert (off["ai"], off["github"], off["memory"]) == (False, False, True)
    assert off["demo_path"] and off["demo_path"].endswith("demo_target")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("CODELOOP_MEMORY", "off")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # the autouse fixture sets CODELOOP_LLM=off: credentials alone do not switch the AI on
    assert client.get("/environment").json()["ai"] is False

    monkeypatch.setenv("CODELOOP_LLM", "on")
    on = client.get("/environment").json()
    assert (on["ai"], on["github"], on["memory"]) == (True, True, False)
