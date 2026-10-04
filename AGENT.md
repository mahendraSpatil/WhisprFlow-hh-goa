# AGENT.md: CodeLoop handoff

Read this first. It says what exists, how to initialise and verify the codebase, the conventions that are easy to get wrong, and what is still open. `README.md` is the user-facing doc; this file is for the next agent.

## What CodeLoop is

An autonomous code-lifecycle system for **Python-only** target repos. Nine agents run in sequence, each with a typed Pydantic input and output, sharing one write-once `RunContext`:

`RepoScout → SystemAnalyst → PipelineArchitect → SandboxRunner → DiagnosticSentinel → RootCauseDiagnostician → PatchMaster → RegressionCheck → MemoryKeeper`

Monorepo: FastAPI backend in `backend/`, React + Vite + TypeScript frontend in `frontend/` (React Flow graph, Monaco diff editor), and `demo_target/`, a small order-processing app with three planted bugs (division by zero, SQL injection, unlocked shared inventory counter). Do not fix the bugs in `demo_target`; it is the demo input and a test fixture.

## Status (as of 2026-10-04)

**Done and verified**

| Area | State |
|---|---|
| All nine agents | Implemented. A real run of `demo_target` completes with all nine stages `success` and no errors, finding all three planted bugs (7 findings, 7 root causes). |
| Orchestrator | Per-stage timeout, one retry, `requires` (hard) vs `uses` (soft) dependencies, statuses `pending/running/success/failed/escalated/skipped`, skip only dependents of a failed stage. |
| API | `POST /runs`, `GET /runs/{id}`, `/graph`, `/diagnostics`, `/patches`, `/regression`, `GET/POST /runs/{id}/pull-request`, `GET /memory/incidents`, `GET /environment`, `WS /ws/runs/{id}?since=N` (seq replay). |
| Frontend | Landing state explaining the nine stages with a one-click demo, skeleton and empty states, stage tracker, graph canvas (failing nodes red, causal chain orange, fixed green, "seen before" badges), finding drawer, Monaco diff view of patches, regression summary card, History tab, log panel, "Create Pull Request" button. |
| GitHub PR flow | PyGithub, Git Data API: one commit per accepted patch, branch `codeloop/fix-{run id}` created last, PR body with root cause, causal chain, before/after tests and hardened design. Idempotent. Missing `GITHUB_TOKEN` gives a clear message, not a crash. |
| Learning loop | SQLite incident memory; signature = sha256(kind \| normalized AST pattern \| OWASP)[:16]; severity boost for repeats; accepted past fixes shown to Claude in PatchMaster. |
| Tests | Backend: **196 passed, 1 skipped** (the skipped one is a slow real-venv test, enable with `CODELOOP_SLOW_TESTS=1`). Frontend: **39 vitest tests passed**, `tsc` clean, `vite build` OK. |
| Git | Repo initialised on `main`, one commit (`a0c6947`), clean tree, **not pushed** (see Open items). |
| README | Written: overview, Mermaid diagram of the nine stages, setup, env vars, safety notes, API, layout, and the "built entirely by voice using Wispr Flow" line. |

**Open items**

1. **Not pushed to GitHub.** The task was "push to a new public repo called `codeloop`". It was blocked: no `gh` CLI, no `GITHUB_TOKEN`/`GH_TOKEN`, no credential helper on the machine. Do not look for stored tokens. Either `gh repo create codeloop --public --source . --remote origin --push` after `gh auth login`, or create the empty repo on github.com and `git remote add origin <url> && git push -u origin main`.
2. **`docs/DESIGN.md` was never written.** It was the user's first request (architecture, per-agent contracts, graph schema, UX, GitHub PR flow, learning loop). The user has not re-raised it. The README does not link to it.
3. **SandboxRunner is subprocess isolation only.** No Docker mode (the original ask said "if Docker is available, run in a python slim container"). No synthetic-input probing or live "simulation view" (traced calls animating graph edges). Both were requested and not built; do not claim them.
4. **Never run against a real Anthropic key.** Claude calls were verified only with a scripted fake client (`FakeClient` in `backend/tests/helpers.py`). Without a key, runs complete but PatchMaster produces no patches (UI says so).
5. **The GitHub flow was never run against real GitHub.** It is tested against an in-memory fake GitHub server (`backend/tests/fake_github.py`) with the real PyGithub client.
6. AGENT.md itself is uncommitted unless someone has committed it since.

## Initialise the codebase

Needs Python 3.11+ (3.12 used), Node 20+, git.

```bash
# backend (port 8000)
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # verified from scratch in a fresh venv
uvicorn app.main:app --port 8000

# frontend (port 5173, proxies /api -> :8000), second terminal
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and press **Analyze the demo app**.

Verify:

```bash
cd backend && .venv/Scripts/python -m pytest tests -q     # ~95 s, expect 196 passed, 1 skipped
cd frontend && npx tsc --noEmit -p . && npm test && npm run build
```

Optional env (full table in README): `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `CODELOOP_LLM=off`, `CODELOOP_MEMORY_DB`, `CODELOOP_GITHUB_REPO`, `GITHUB_API_URL`, `CODELOOP_SANDBOX_PYTHON`.

## Layout

```
backend/app/
  main.py                create_app(); routers; app.state.{orchestrator,runs,github_factory}
  models/                agents.py (every agent contract), run.py (RunContext), events.py, memory.py, pr.py
  orchestrator/          runner.py (timeouts/retry/escalation), registry.py (RunHandle), events.py (stream + replay)
  agents/                one module per agent + helpers: indexer, graph_index, bandit_scan, owasp, race_check,
                         sandbox_findings, explainers, llm (ClaudeClient), patching (git apply), base
  api/                   runs.py, memory.py, pulls.py, environment.py
  sandbox.py             isolated pytest runs; sandbox_harness/ is the plugin + guard injected into the target venv
  github_pr.py           repo detection, PR body, commits, error mapping
  memory.py              SQLite store; graph_overlay.py marks failing/fixed nodes
backend/tests/           NOT a package: import siblings as `from helpers import ...`, `from fake_github import ...`
frontend/src/
  api/                   types.ts mirrors backend models; client.ts (ApiError, request())
  state/                 runReducer.ts (all run state), useRun.ts (WebSocket + request tickets)
  components/            Landing, Skeletons, StageTracker, GraphCanvas, CodeNode, FindingDrawer, PatchView,
                         RegressionCard, PullRequestAction, HistoryView, LogPanel, TopBar
  *.ts (+ .test.ts)      pure helpers: stages, diagnostics, patches, regression, history, pullRequest, capabilities
demo_target/             the planted-bug app and its own pytest suite
```

## How it fits together

- **Contracts:** each agent has `<Name>Input`/`<Name>Output` in `models/agents.py`; outputs are stored write-once on `RunContext` (`ctx.set_output`). `ctx.fetch_target` is the unredacted clone URL (never serialised); `ctx.source` is redacted.
- **Graph ids:** `fn:{qualname}`, `main:{module}`, `res:{lib}`. Findings have stable hash ids; node status (`failing`, `fixed`) is overlaid at read time in `graph_overlay.py`.
- **Claude:** `agents/llm.py`, model `claude-opus-5-5` by default, falls back to deterministic templates when disabled or no credentials. PatchMaster validates diffs (in-place edits only, at most 3 files, no `..` or symlinks), runs `git apply --check --recount`, retries once with git's error.
- **RegressionCheck:** evaluates each patch in isolation, then all accepted together; verdicts `PASS/REGRESSED/APPLY_FAILED`; finding identity key is `(rule, file, function)`. `summary.patches_applied` is the order the PR commits use.
- **Frontend data flow:** WebSocket events drive `runReducer`; graph, diagnostics, patches and regression are fetched when their stage succeeds.

## Conventions and traps

- **Tests must never touch the real world.** `conftest.py` autouse fixtures force `CODELOOP_LLM=off`, point `CODELOOP_MEMORY_DB` at a temp file, set `CODELOOP_SANDBOX_PYTHON` to the current interpreter, and clear `GITHUB_TOKEN`/`GITHUB_API_URL`/`CODELOOP_GITHUB_REPO` and zero PyGithub's pacing. Never use `monkeypatch.undo()` in a test (it once removed the memory isolation and wrote to the real `~/.codeloop`).
- **PyGithub:** `repo.get_git_ref()` is lazy and never raises. Check branch existence with `get_branch`. The client sends `Authorization: token <t>`. Real runs are paced (0.25 s between requests, 1 s between writes, via `SECONDS_BETWEEN_*` in `github_pr.py`), so a PR takes ~10-15 s.
- **Line endings (Windows):** write fixture files as bytes (`write_bytes`) so patches see real LF; send diffs to `git apply` as bytes on stdin. `.gitattributes` normalises to LF in the repo. `styles.css` and several files are CRLF in the working tree; edit with tools that preserve the existing ending.
- **Bash tool:** long heredocs with quotes can fail to parse; write files with the editor tools instead.
- **Sandbox:** target tests run from a private copy in a fresh venv (or `CODELOOP_SANDBOX_PYTHON`), events travel through a file named by `CODELOOP_EVENTS`, not stdout. It is best-effort isolation, not a security boundary; keep the wording honest.
- **Don't send repo code to the API in tests or demos without `CODELOOP_LLM` consideration.** Visual checks used scripted fakes, not real Claude.
- Pull request text written by a model goes through `md_text()` (escapes HTML, `@`, `#NN`, link targets). Keep that for any new field added to the body.
- Frontend: pure logic lives in plain `.ts` files with vitest tests; components stay thin. Both views (analysis and history) stay mounted and are hidden with a class, so the graph viewport survives tab switches.

## Quick end-to-end check

Start the backend, then in another shell run the helper pattern: `POST /runs {"source": "<abs path to demo_target>"}`, poll `GET /runs/{id}` until `completed`, and expect nine `success` stages, `GET /diagnostics` with 7 findings, and `GET /regression` with no verdicts when no API key is set.
