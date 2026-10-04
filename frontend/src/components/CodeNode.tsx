import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { Box, Braces, Check, Database, DoorOpen, Globe, History as HistoryIcon } from "lucide-react";
import { memo, type CSSProperties } from "react";

import type { GraphNode, NodeType } from "../api/types";
import { LAYER_LABELS, nodeColor } from "../stages";

export type CodeNodeData = { node: GraphNode; dimmed: boolean; onChain: boolean; focused: boolean };
export type CodeFlowNode = Node<CodeNodeData, "code">;

export type ColumnNodeData = { title: string; count: number; color: string | null };
export type ColumnFlowNode = Node<ColumnNodeData, "column">;

const TYPE_ICONS: Record<NodeType, typeof Box> = {
  entry: DoorOpen,
  function: Braces,
  service: Box,
  database: Database,
  external: Globe,
};

const TYPE_LABELS: Record<NodeType, string> = {
  entry: "entry",
  function: "function",
  service: "service",
  database: "database",
  external: "external",
};

function CodeNodeCard({ data, selected }: NodeProps<CodeFlowNode>) {
  const { node, dimmed } = data;
  const Icon = TYPE_ICONS[node.type];
  const color = nodeColor(node.type, node.layer);
  const isResource = node.type === "database" || node.type === "external";
  const m = node.metrics;
  const location = node.file ? `${node.file}:${node.start_line}` : null;

  return (
    <div
      className={[
        "code-node",
        `code-node--${node.type}`,
        `code-node--status-${node.status}`,
        selected ? "is-selected" : "",
        dimmed ? "is-dimmed" : "",
        data.onChain ? "is-chain" : "",
        data.focused ? "is-focused" : "",
      ].join(" ")}
      style={{ "--node-color": color } as CSSProperties}
      title={node.symbol ?? node.label}
    >
      <Handle type="target" position={Position.Left} className="code-node__handle" />
      <div className="code-node__head">
        <span className="code-node__icon">
          <Icon size={13} strokeWidth={2.2} />
        </span>
        <span className="code-node__kind">{TYPE_LABELS[node.type]}</span>
        {node.layer && !isResource && <span className="code-node__layer">{LAYER_LABELS[node.layer]}</span>}
        {m.seen_before > 0 && (
          <span
            className="code-node__seen"
            title={`A pattern in this node was already recorded in ${m.seen_before} earlier run${m.seen_before === 1 ? "" : "s"}`}
          >
            <HistoryIcon size={10} strokeWidth={2.6} /> seen ×{m.seen_before}
          </span>
        )}
        {node.status === "fixed" && (
          <span className="code-node__fixed" title="A patch for this node was verified on a fresh copy of the repo">
            <Check size={11} strokeWidth={3} /> fixed
          </span>
        )}
        <span className={`code-node__status code-node__status--${node.status}`} title={`status: ${node.status}`} />
      </div>
      <div className="code-node__name">{node.label}</div>
      {location ? (
        <div className="code-node__file">{location}</div>
      ) : (
        <div className="code-node__file">{node.symbol}</div>
      )}
      <div className="code-node__metrics">
        {isResource ? (
          <span>
            {m.fan_in} caller{m.fan_in === 1 ? "" : "s"} · {m.call_sites} call site{m.call_sites === 1 ? "" : "s"}
          </span>
        ) : (
          <>
            {m.complexity !== null && <span title="cyclomatic complexity">cc {m.complexity}</span>}
            {m.loc !== null && <span title="lines of code">{m.loc} loc</span>}
            <span title="incoming / outgoing edges">
              ↘{m.fan_in} ↗{m.fan_out}
            </span>
            {m.findings > 0 && (
              <span className={node.status === "fixed" ? "code-node__findings code-node__findings--fixed" : "code-node__findings"}>
                {node.status === "fixed" ? "resolved " : ""}
                {m.findings} finding{m.findings === 1 ? "" : "s"}
              </span>
            )}
          </>
        )}
      </div>
      <Handle type="source" position={Position.Right} className="code-node__handle" />
    </div>
  );
}

function ColumnHeader({ data }: NodeProps<ColumnFlowNode>) {
  return (
    <div className="column-header" style={{ "--node-color": data.color ?? "var(--text-muted)" } as CSSProperties}>
      <span className="column-header__title">{data.title}</span>
      <span className="column-header__count">{data.count}</span>
    </div>
  );
}

export const CodeNode = memo(CodeNodeCard);
export const ColumnNode = memo(ColumnHeader);
