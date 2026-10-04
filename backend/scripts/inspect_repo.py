"""Run RepoScout, SystemAnalyst and PipelineArchitect on a repo and print what they found.

    python scripts/inspect_repo.py ../demo_target
    python scripts/inspect_repo.py https://github.com/org/repo.git --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from app.agents.pipeline_architect import PipelineArchitect
from app.agents.repo_scout import RepoScout
from app.agents.system_analyst import SystemAnalyst
from app.models.agents import CallResolution, Graph, PipelineArchitectOutput, RepoScoutOutput, SystemAnalystOutput
from app.models.events import LogEvent
from app.models.run import RunContext, StageName, StageStatus
from app.orchestrator.runner import Orchestrator
from app.sources import InvalidSource, classify_source


async def run(source: str, workspace: Path) -> RunContext:
    kind, target = classify_source(source)
    orchestrator = Orchestrator(workspace_root=workspace, agents=[RepoScout(), SystemAnalyst(), PipelineArchitect()])
    ctx, events = orchestrator.create_run(kind, target)

    async def print_logs() -> None:
        async for event in events.subscribe():
            if isinstance(event, LogEvent):
                print(f"  [{event.stage or 'run'}] {event.message}")

    printer = asyncio.create_task(print_logs())
    await orchestrator.execute(ctx, events)
    await printer
    return ctx


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def print_repo(repo: RepoScoutOutput, system: SystemAnalystOutput | None) -> None:
    heading("RepoScout: modules")
    kinds_by_module: dict[str, Counter[str]] = defaultdict(Counter)
    for s in repo.symbols:
        kinds_by_module[s.module][s.kind.value] += 1
    print(f"{'module':32} {'layer':8} {'loc':>4}  symbols                 depends on")
    for m in repo.modules:
        layer = system.layer_of(m.name) if system else None
        counts = ", ".join(f"{n} {k}" for k, n in sorted(kinds_by_module[m.name].items())) or "-"
        print(f"{m.name:32} {layer or '?':8} {m.loc:>4}  {counts:23} {', '.join(m.depends_on) or '-'}")

    heading("RepoScout: symbols")
    for s in repo.symbols:
        if s.module.startswith("tests"):
            continue
        extra = f"  bases={s.bases}" if s.bases else ""
        print(f"  {s.location.file}:{s.location.line:<4} {s.kind.value:8} {s.qualname}{extra}")

    heading("RepoScout: internal call graph (caller -> callee)")
    edges = sorted({(c.caller, c.target) for c in repo.calls if c.resolution is CallResolution.INTERNAL})
    for caller, target in edges:
        print(f"  {caller} -> {target}")

    heading("RepoScout: external calls by target")
    external = Counter(c.target for c in repo.calls if c.resolution is CallResolution.EXTERNAL)
    for target, n in sorted(external.items(), key=lambda kv: (-kv[1], kv[0])):
        if not target.startswith("builtins."):
            print(f"  {n:3}x {target}")
    unresolved = sum(c.resolution is CallResolution.UNRESOLVED for c in repo.calls)
    print(f"  ({sum(1 for t in external if t.startswith('builtins.'))} builtins omitted; {unresolved} calls unresolved)")


def print_system(system: SystemAnalystOutput) -> None:
    heading("SystemAnalyst: stack")
    for c in system.stack:
        where = ", ".join(w for w, on in (("declared", c.declared), ("app", c.used_in_app), ("tests", c.used_in_tests)) if on)
        print(f"  {c.name:28} {c.category.value:14} [{where}]")
        for e in c.evidence[:2]:
            print(f"      {e}")

    heading("SystemAnalyst: layers")
    for l in system.layers:
        print(f"  {l.module:32} {l.layer.value:8} {l.confidence:.2f}  {'; '.join(l.evidence)}")
    for d in system.layer_dependencies:
        print(f"  {d.source.value} -> {d.target.value}: {d.imports} import(s){'  UPWARD' if d.upward else ''}")

    heading("SystemAnalyst: entrypoints and tests")
    for e in system.entrypoints:
        print(f"  {e.kind.value:12} {e.symbol or e.module}  ({e.location.file}:{e.location.line})")
    print(f"  test runner: {system.test_runner}, paths: {system.test_paths}")
    print(f"  python: {system.python_requires}, dependency files: {system.dependency_files}")

    heading("SystemAnalyst: summary")
    print(f"  {system.summary}")


def print_graph(graph: Graph) -> None:
    heading("PipelineArchitect: graph columns (left to right)")
    for column in graph.columns:
        nodes = sorted((n for n in graph.nodes if n.position.x == column.x), key=lambda n: n.position.y)
        print(f"  x={column.x:<6.0f} {column.title}")
        for n in nodes:
            m = n.metrics
            where = f"{n.file}:{n.start_line}-{n.end_line}" if n.file else ""
            stats = f"loc={m.loc} cc={m.complexity} " if m.loc else ""
            print(f"      y={n.position.y:<6.0f} [{n.type.value:8}] {n.label:36} {where:24} {stats}in={m.fan_in} out={m.fan_out}")

    heading("PipelineArchitect: edges")
    label = {n.id: n.label for n in graph.nodes}
    for e in sorted(graph.edges, key=lambda e: (e.kind.value, label[e.source])):
        print(f"  {e.kind.value:6} {'(animated) ' if e.animated else ''}{label[e.source]} -> {label[e.target]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="git URL or local directory")
    parser.add_argument("--json", type=Path, help="also write both agents' outputs to this file")
    args = parser.parse_args()
    try:
        classify_source(args.source)
    except InvalidSource as exc:
        parser.error(str(exc))

    with tempfile.TemporaryDirectory(prefix="codeloop-inspect-") as tmp:
        print(f"Inspecting {args.source}")
        ctx = asyncio.run(run(args.source, Path(tmp)))
        for record in ctx.stages.values():
            if record.status is not StageStatus.SUCCESS:
                print(f"\n{record.title}: {record.status}", *(f"  {e.type}: {e.message}" for e in record.errors), sep="\n")
        repo = ctx.optional(StageName.REPO_SCOUT, RepoScoutOutput)
        system = ctx.optional(StageName.SYSTEM_ANALYST, SystemAnalystOutput)
        if repo:
            print_repo(repo, system)
        architect = ctx.optional(StageName.PIPELINE_ARCHITECT, PipelineArchitectOutput)
        if system:
            print_system(system)
        if architect:
            print_graph(architect.graph)
        if args.json:
            payload = {
                "repo_scout": repo and repo.model_dump(mode="json"),
                "system_analyst": system and system.model_dump(mode="json"),
                "graph": architect and architect.graph.model_dump(mode="json"),
            }
            args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"\nWrote {args.json}")
        return 0 if repo and system and architect else 1


if __name__ == "__main__":
    sys.exit(main())
