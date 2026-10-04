import { Loader2 } from "lucide-react";
import type { CSSProperties } from "react";

import { LAYER_COLORS } from "../stages";

/** Column layout of a typical graph: entry, api, service, data. Heights are cards per column. */
const COLUMNS: { layer: keyof typeof LAYER_COLORS; cards: number }[] = [
  { layer: "entry", cards: 1 },
  { layer: "api", cards: 4 },
  { layer: "service", cards: 5 },
  { layer: "data", cards: 3 },
];

/** A shimmering stand-in for the graph while the first stages run, so the canvas does not jump when it arrives. */
export function CanvasSkeleton({ title, body }: { title: string; body: string }) {
  return (
    <div className="canvas canvas--skeleton" role="status" aria-live="polite" aria-label={title}>
      <div className="skel-graph" aria-hidden>
        {COLUMNS.map((column, c) => (
          <div key={column.layer} className="skel-graph__col">
            <div className="skeleton skeleton--heading" style={{ "--skel-tint": LAYER_COLORS[column.layer] } as CSSProperties} />
            {Array.from({ length: column.cards }, (_, i) => (
              <div key={i} className="skeleton skeleton--card" style={{ animationDelay: `${(c * 0.12 + i * 0.07).toFixed(2)}s` }}>
                <span className="skeleton skeleton--line skeleton--w60" />
                <span className="skeleton skeleton--line skeleton--w85" />
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="skel-caption">
        <Loader2 size={15} className="spin" />
        <div>
          <div className="skel-caption__title">{title}</div>
          <div className="skel-caption__body">{body}</div>
        </div>
      </div>
    </div>
  );
}

const LOG_WIDTHS = ["w85", "w60", "w75", "w40", "w85", "w60"];

export function LogSkeleton() {
  return (
    <div className="logs__skeleton" role="status" aria-label="Waiting for output">
      {LOG_WIDTHS.map((w, i) => (
        <div key={i} className="logs__skeleton-row" aria-hidden>
          <span className="skeleton skeleton--time" />
          <span className={`skeleton skeleton--line skeleton--${w}`} />
        </div>
      ))}
    </div>
  );
}

export function IncidentSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <ul className="incidents incidents--skeleton" role="status" aria-label="Loading incidents">
      {Array.from({ length: rows }, (_, i) => (
        <li key={i} className="incident incident--skeleton" aria-hidden>
          <span className="skeleton skeleton--badge" />
          <span className="incident__what">
            <span className={`skeleton skeleton--line skeleton--${i % 2 ? "w60" : "w75"}`} />
            <span className="skeleton skeleton--line skeleton--w40" />
          </span>
          <span className="skeleton skeleton--badge" />
        </li>
      ))}
    </ul>
  );
}
