import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  Panel,
  ReactFlow,
  type Edge,
  type NodeTypes,
} from "@xyflow/react";
import { AlertTriangle, CircleCheck } from "lucide-react";
import { useMemo, useState } from "react";

import type { Diagnostics, EdgeKind, Graph, GraphNode, Layer, Patch, RegressionCheckOutput, StageStatus } from "../api/types";
import { chainEdgeIds, chainNodeIds, findingsForNode } from "../diagnostics";
import type { RunState } from "../state/runReducer";
import { LAYER_COLORS, LAYER_LABELS, TYPE_COLORS, nodeColor } from "../stages";
import { CodeNode, ColumnNode, type CodeFlowNode, type ColumnFlowNode } from "./CodeNode";
import { FindingDrawer } from "./FindingDrawer";
import { Landing } from "./Landing";
import { CanvasSkeleton } from "./Skeletons";

const nodeTypes: NodeTypes = { code: CodeNode, column: ColumnNode };

// Card size (see .code-node). The graph is read-only, so measured sizes never flow back
// into the node objects; the minimap and fitView use these until the DOM is measured.
const CARD_WIDTH = 260;
const CARD_HEIGHT = 100;

const EDGE_COLORS: Record<EdgeKind, string> = {
  call: "#5b6478",
  data: "#ff8a4c",
  import: "#4a5163",
};
const CHAIN_COLOR = "#ff9f43";

const COLUMN_COLORS: Record<string, string> = {
  ...LAYER_COLORS,
  database: TYPE_COLORS.database!,
  external: TYPE_COLORS.external!,
};

type FlowNode = CodeFlowNode | ColumnFlowNode;

interface Emphasis {
  hovered: string | null;
  /** Nodes and edges on a causal chain (the selected finding's, or all of them). */
  chainNodes: Set<string>;
  chainEdges: Set<string>;
  focused: string | null;
}

function toFlow(graph: Graph, { hovered, chainNodes, chainEdges, focused }: Emphasis): { nodes: FlowNode[]; edges: Edge[] } {
  // When a node is hovered, everything not directly connected to it fades back.
  const linked = new Set<string>();
  if (hovered) {
    linked.add(hovered);
    for (const e of graph.edges) {
      if (e.source === hovered) linked.add(e.target);
      if (e.target === hovered) linked.add(e.source);
    }
  }

  const columnNodes: ColumnFlowNode[] = graph.columns.map((c) => ({
    id: `column:${c.key}`,
    type: "column",
    position: { x: c.x, y: -76 },
    data: {
      title: c.title,
      count: graph.nodes.filter((n) => n.position.x === c.x).length,
      color: COLUMN_COLORS[c.key] ?? null,
    },
    draggable: false,
    selectable: false,
    focusable: false,
  }));

  const codeNodes: CodeFlowNode[] = graph.nodes.map((n) => ({
    id: n.id,
    type: "code",
    position: n.position,
    initialWidth: CARD_WIDTH,
    initialHeight: CARD_HEIGHT,
    data: {
      node: n,
      dimmed: hovered !== null && !linked.has(n.id),
      onChain: chainNodes.has(n.id),
      focused: focused === n.id,
    },
  }));

  const edges: Edge[] = graph.edges.map((e) => {
    const active = hovered !== null && (e.source === hovered || e.target === hovered);
    const onChain = chainEdges.has(e.id);
    const color = onChain ? CHAIN_COLOR : active ? "#c3cbe0" : EDGE_COLORS[e.kind];
    const classes = ["flow-edge", `flow-edge--${e.kind}`];
    if (active) classes.push("is-active");
    if (onChain) classes.push("flow-edge--chain");
    if (hovered && !active && !onChain) classes.push("is-dimmed");
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      animated: e.animated,
      className: classes.join(" "),
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
      style: { stroke: color },
      zIndex: onChain ? 12 : active ? 10 : 0,
    };
  });

  return { nodes: [...columnNodes, ...codeNodes], edges };
}

interface GraphCanvasProps {
  run: RunState;
  onSelectNode: (nodeId: string | null) => void;
  onRun: (source: string) => void;
}

export function GraphCanvas({ run, onSelectNode, onRun }: GraphCanvasProps) {
  if (!run.graph) return <EmptyCanvas run={run} onRun={onRun} />;
  // Remount per run so the viewport fits the new graph.
  return (
    <FlowView
      key={run.runId ?? "graph"}
      graph={run.graph}
      diagnostics={run.diagnostics}
      patches={run.patches}
      patchStage={run.stages.patch_master.status}
      regression={run.regression}
      regressionStage={run.stages.regression_check.status}
      selectedNodeId={run.selectedNodeId}
      onSelectNode={onSelectNode}
    />
  );
}

interface FlowViewProps {
  graph: Graph;
  diagnostics: Diagnostics | null;
  patches: Patch[] | null;
  patchStage: StageStatus;
  regression: RegressionCheckOutput | null;
  regressionStage: StageStatus;
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
}

function FlowView({
  graph,
  diagnostics,
  patches,
  patchStage,
  regression,
  regressionStage,
  selectedNodeId,
  onSelectNode,
}: FlowViewProps) {
  const [hovered, setHovered] = useState<string | null>(null);
  const drawerOpen = selectedNodeId !== null && findingsForNode(diagnostics, selectedNodeId).length > 0;
  const focused = drawerOpen ? selectedNodeId : null;
  // Causal chains are drawn for problems that are still open: once a node is fixed its chain stops lighting up.
  const open = useMemo(() => {
    if (!diagnostics) return null;
    const fixed = new Set(graph.nodes.filter((n) => n.status === "fixed").map((n) => n.id));
    return { ...diagnostics, root_causes: diagnostics.root_causes.filter((c) => !c.node_id || !fixed.has(c.node_id)) };
  }, [graph, diagnostics]);
  const chainNodes = useMemo(() => chainNodeIds(open, focused), [open, focused]);
  const chainEdges = useMemo(() => chainEdgeIds(graph, open, focused), [graph, open, focused]);
  const anyFixed = useMemo(() => graph.nodes.some((n) => n.status === "fixed"), [graph]);
  const { nodes, edges } = useMemo(
    () => toFlow(graph, { hovered, chainNodes, chainEdges, focused }),
    [graph, hovered, chainNodes, chainEdges, focused],
  );
  const failing = useMemo(() => graph.nodes.some((n) => n.status === "failing"), [graph]);
  const findingCount = diagnostics?.findings.length ?? 0;
  const layers = useMemo(() => {
    const present = new Set(graph.nodes.map((n) => n.layer).filter((l): l is Layer => l !== null));
    return (Object.keys(LAYER_COLORS) as Layer[]).filter((l) => present.has(l));
  }, [graph]);

  return (
    <div className={drawerOpen ? "canvas canvas--drawer" : "canvas"}>
      <ReactFlow<FlowNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        colorMode="dark"
        fitView
        fitViewOptions={{ padding: 0.12, maxZoom: 1.1 }}
        minZoom={0.15}
        maxZoom={2}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        onNodeMouseEnter={(_, node) => node.type === "code" && setHovered(node.id)}
        onNodeMouseLeave={() => setHovered(null)}
        onNodeClick={(_, node) => {
          // Only nodes carrying findings open the drawer; any other click dismisses it.
          const clickable = node.type === "code" && findingsForNode(diagnostics, node.id).length > 0;
          onSelectNode(clickable ? node.id : null);
        }}
        onPaneClick={() => onSelectNode(null)}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#252a36" />
        <Controls showInteractive={false} position="top-right" orientation="horizontal" />
        <MiniMap
          position="bottom-right"
          pannable
          zoomable
          nodeColor={(n) =>
            n.type === "code" ? nodeColor((n.data as { node: GraphNode }).node.type, (n.data as { node: GraphNode }).node.layer) : "transparent"
          }
          nodeStrokeWidth={0}
          nodeBorderRadius={3}
          maskColor="rgba(8, 10, 14, 0.72)"
        />
        <Panel position="bottom-left" className="legend">
          <div className="legend__group">
            {layers.map((l) => (
              <span key={l} className="legend__item">
                <span className="legend__swatch" style={{ background: LAYER_COLORS[l] }} />
                {LAYER_LABELS[l]}
              </span>
            ))}
            {graph.nodes.some((n) => n.type === "database") && (
              <span className="legend__item">
                <span className="legend__swatch" style={{ background: TYPE_COLORS.database }} />
                Database
              </span>
            )}
            {graph.nodes.some((n) => n.type === "external") && (
              <span className="legend__item">
                <span className="legend__swatch" style={{ background: TYPE_COLORS.external }} />
                External
              </span>
            )}
          </div>
          <div className="legend__group">
            <span className="legend__item">
              <span className="legend__line legend__line--call" /> call
            </span>
            <span className="legend__item">
              <span className="legend__line legend__line--data" /> data
            </span>
            <span className="legend__item">
              <span className="legend__line legend__line--import" /> import
            </span>
          </div>
          {(failing || anyFixed || chainNodes.size > 0) && (
            <div className="legend__group">
              {anyFixed && (
                <span className="legend__item">
                  <span className="legend__swatch legend__swatch--fixed" /> Fixed
                </span>
              )}
              {failing && (
                <span className="legend__item">
                  <span className="legend__swatch legend__swatch--failing" /> Failing
                </span>
              )}
              {chainNodes.size > 0 && (
                <span className="legend__item">
                  <span className="legend__swatch legend__swatch--chain" /> Causal chain
                </span>
              )}
            </div>
          )}
        </Panel>
        {diagnostics && findingCount === 0 && (
          <Panel position="top-center" className="canvas__clean">
            <CircleCheck size={14} /> No issues found: tests pass, the security scan is clean and no shared state is unguarded.
          </Panel>
        )}
        <Panel position="top-left" className="canvas__stats">
          {graph.nodes.length} nodes · {graph.edges.length} edges
          {findingCount > 0 && <span className="canvas__findings"> · {findingCount} findings</span>}
        </Panel>
      </ReactFlow>
      {drawerOpen && selectedNodeId && (
        <FindingDrawer
          node={graph.nodes.find((n) => n.id === selectedNodeId)}
          diagnostics={diagnostics}
          patches={patches}
          patchStage={patchStage}
          regression={regression}
          regressionStage={regressionStage}
          nodeId={selectedNodeId}
          onClose={() => onSelectNode(null)}
        />
      )}
    </div>
  );
}

function EmptyCanvas({ run, onRun }: { run: RunState; onRun: (source: string) => void }) {
  const architect = run.stages.pipeline_architect;

  if (run.runId && (run.graphState === "unavailable" || architect.status === "escalated" || architect.status === "skipped")) {
    const body =
      architect.error?.message ?? architect.skippedReason ?? run.graphMessage ?? "PipelineArchitect did not produce a graph for this run.";
    return (
      <div className="canvas canvas--empty">
        <div className="empty">
          <div className="empty__icon">
            <AlertTriangle size={36} />
          </div>
          <div className="empty__title">Graph unavailable</div>
          <div className="empty__body">{body}</div>
        </div>
      </div>
    );
  }

  if (!run.runId && run.status === "idle") return <Landing onRun={onRun} />;

  if (architect.status === "running" || run.graphState === "loading") {
    return <CanvasSkeleton title="Building the graph" body="PipelineArchitect is laying out the data flow." />;
  }
  return (
    <CanvasSkeleton
      title={run.runId ? "Analyzing the repository" : "Starting the run"}
      body={
        run.runId
          ? "RepoScout and SystemAnalyst are indexing the code. The graph comes next."
          : "Creating the run and connecting to its event stream."
      }
    />
  );
}
