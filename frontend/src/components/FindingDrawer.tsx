import {
  Bot,
  ChevronRight,
  FileCode2,
  FileDiff,
  History as HistoryIcon,
  Lightbulb,
  Loader2,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sparkles,
  X,
} from "lucide-react";
import { Fragment, Suspense, lazy, useEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

import type {
  ChainStep,
  CodeSnippet,
  Diagnostics,
  Finding,
  GraphNode,
  Patch,
  PatchVerdict,
  RegressionCheckOutput,
  RootCause,
  StageStatus,
} from "../api/types";
import { causesForNode, findingsForNode, tokenizeLine, worstSeverity } from "../diagnostics";
import { diffSummary, patchForCause } from "../patches";
import { verdictFor, verifiedSummary } from "../regression";
import { CATEGORY_LABELS, SEVERITY_COLORS, SOURCE_LABELS } from "../stages";

// Monaco is large; it downloads only when someone opens a patch.
const PatchView = lazy(() => import("./PatchView"));

interface FindingDrawerProps {
  node: GraphNode | undefined;
  diagnostics: Diagnostics | null;
  patches: Patch[] | null;
  patchStage: StageStatus;
  regression: RegressionCheckOutput | null;
  regressionStage: StageStatus;
  nodeId: string;
  onClose: () => void;
}

export function FindingDrawer({
  node,
  diagnostics,
  patches,
  patchStage,
  regression,
  regressionStage,
  nodeId,
  onClose,
}: FindingDrawerProps) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const [viewing, setViewing] = useState<Patch | null>(null);
  const findings = findingsForNode(diagnostics, nodeId);
  const causes = causesForNode(diagnostics, nodeId);
  const covered = new Set(causes.flatMap((c) => c.finding_ids));
  const loose = findings.filter((f) => !covered.has(f.id));
  const worst = worstSeverity(findings);

  useEffect(() => {
    closeButton.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, nodeId]);

  return (
    <aside className="drawer" role="dialog" aria-label={`Findings for ${node?.label ?? nodeId}`}>
      <header className="drawer__header">
        <div className="drawer__heading">
          <div className="drawer__eyebrow">
            <ShieldAlert size={13} />
            {findings.length} finding{findings.length === 1 ? "" : "s"}
            {worst && (
              <span className="badge" style={{ "--badge-color": SEVERITY_COLORS[worst] } as CSSProperties}>
                {worst}
              </span>
            )}
          </div>
          <h2 className="drawer__title">{node?.label ?? nodeId}</h2>
          {node?.file && (
            <div className="drawer__location">
              {node.file}:{node.start_line}
              {node.end_line && node.end_line !== node.start_line ? `-${node.end_line}` : ""}
            </div>
          )}
        </div>
        <button ref={closeButton} className="icon-btn" onClick={onClose} aria-label="Close" title="Close (Esc)">
          <X size={17} />
        </button>
      </header>

      <div className="drawer__body">
        {causes.map((cause) => (
          <CauseSection
            key={cause.id}
            cause={cause}
            findings={findings.filter((f) => cause.finding_ids.includes(f.id))}
            patch={patchForCause(patches, cause.id)}
            patchStage={patchStage}
            verdict={verdictFor(regression, patchForCause(patches, cause.id)?.id ?? "")}
            regressionStage={regressionStage}
            onViewPatch={setViewing}
          />
        ))}
        {loose.length > 0 && (
          <section className="drawer__section">
            {loose.map((f) => (
              <FindingCard key={f.id} finding={f} />
            ))}
            <p className="drawer__pending">
              {diagnostics?.root_causes_ready ? (
                "No root cause was produced for this finding."
              ) : (
                <>
                  <Loader2 size={14} className="spin" /> Tracing the root cause…
                </>
              )}
            </p>
          </section>
        )}
      </div>
      {viewing &&
        createPortal(
          <Suspense
            fallback={
              <div className="patchview patchview--loading">
                <Loader2 size={20} className="spin" /> Loading the diff editor…
              </div>
            }
          >
            <PatchView
              patch={viewing}
              verdict={verdictFor(regression, viewing.id)}
              title={node?.label ?? nodeId}
              onClose={() => setViewing(null)}
            />
          </Suspense>,
          document.body,
        )}
    </aside>
  );
}

interface CauseSectionProps {
  cause: RootCause;
  findings: Finding[];
  patch: Patch | undefined;
  patchStage: StageStatus;
  verdict: PatchVerdict | undefined;
  regressionStage: StageStatus;
  onViewPatch: (patch: Patch) => void;
}

function CauseSection({ cause, findings, patch, patchStage, verdict, regressionStage, onViewPatch }: CauseSectionProps) {
  return (
    <section className="drawer__section">
      {findings.map((f) => (
        <FindingCard key={f.id} finding={f} />
      ))}

      <h3 className="drawer__label">Causal chain</h3>
      {cause.chain.length > 0 ? (
        <Breadcrumb chain={cause.chain} />
      ) : (
        <p className="drawer__muted">No path from an entry point to this code was found in the graph.</p>
      )}

      <h3 className="drawer__label">
        Root cause
        <span className={`origin origin--${cause.explanation_source}`} title={originTitle(cause)}>
          {cause.explanation_source === "claude" ? <Sparkles size={11} /> : <Bot size={11} />}
          {cause.explanation_source === "claude" ? "Claude" : "Template"}
        </span>
        <span className="drawer__confidence" title="How much evidence supports this diagnosis">
          {Math.round(cause.confidence * 100)}% confidence
        </span>
      </h3>
      <p className="drawer__explanation">{cause.explanation}</p>

      <h3 className="drawer__label">
        <FileDiff size={13} /> Patch
      </h3>
      <PatchBlock patch={patch} stage={patchStage} verdict={verdict} regressionStage={regressionStage} onView={onViewPatch} />

      {cause.snippet && (
        <>
          <h3 className="drawer__label">
            <FileCode2 size={13} /> Code
            <span className="drawer__file">
              {cause.snippet.file}:{cause.snippet.highlight_line}
            </span>
          </h3>
          <CodeBlock snippet={cause.snippet} />
        </>
      )}
    </section>
  );
}

interface PatchBlockProps {
  patch: Patch | undefined;
  stage: StageStatus;
  verdict: PatchVerdict | undefined;
  regressionStage: StageStatus;
  onView: (p: Patch) => void;
}

function PatchBlock({ patch, stage, verdict, regressionStage, onView }: PatchBlockProps) {
  if (!patch) {
    if (stage === "escalated" || stage === "skipped") {
      return <p className="drawer__muted">PatchMaster did not run, so there is no patch for this finding.</p>;
    }
    return (
      <p className="drawer__pending">
        <Loader2 size={14} className="spin" /> {stage === "success" ? "Loading the patch…" : "Waiting for PatchMaster…"}
      </p>
    );
  }
  if (patch.status === "valid") {
    return (
      <div>
        <div className="patchbox">
          <button className="btn btn--primary btn--sm" onClick={() => onView(patch)}>
            <FileDiff size={14} /> View Patch
          </button>
          <span className="patchbox__meta">{diffSummary(patch)}</span>
          {patch.memory_examples > 0 && (
            <span className="patchbox__memory" title="Accepted fixes from earlier incidents with a similar signature were shown to Claude as examples">
              <HistoryIcon size={11} /> informed by {patch.memory_examples} past fix{patch.memory_examples === 1 ? "" : "es"}
            </span>
          )}
        </div>
        <Verification verdict={verdict} stage={regressionStage} />
      </div>
    );
  }
  return (
    <div className={`patchbox patchbox--${patch.status}`}>
      <p className="patchbox__note">
        {patch.status === "invalid"
          ? "Claude's diff did not apply, even after a retry, so it is not offered."
          : "No patch was produced."}{" "}
        {patch.detail && <span className="patchbox__detail">{patch.detail.split("\n")[0]}</span>}
      </p>
      {patch.design_suggestion && (
        <p className="patchbox__design">
          <Lightbulb size={13} /> {patch.design_suggestion}
        </p>
      )}
    </div>
  );
}

/** What RegressionCheck found out about a patch: verified, rejected with the reason, or still pending. */
function Verification({ verdict, stage }: { verdict: PatchVerdict | undefined; stage: StageStatus }) {
  if (!verdict) {
    if (stage === "escalated" || stage === "skipped") {
      return <p className="verify verify--muted">This patch was not verified: RegressionCheck did not run.</p>;
    }
    return (
      <p className="verify verify--muted">
        <Loader2 size={13} className="spin" /> {stage === "success" ? "Loading the result…" : "Verifying on a fresh copy…"}
      </p>
    );
  }
  if (verdict.accepted) {
    return (
      <div className="verify verify--good">
        <div className="verify__line">
          <ShieldCheck size={14} /> <strong>Verified:</strong> {verifiedSummary(verdict)}
        </div>
        {verdict.reasons.map((r) => (
          <div key={r} className="verify__note">
            {r}
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="verify verify--bad">
      <div className="verify__line">
        <ShieldX size={14} /> <strong>Rejected:</strong> {verdict.verdict === "apply_failed" ? "does not apply" : "causes a regression"}
      </div>
      {verdict.reasons.map((r) => (
        <div key={r} className="verify__note">
          {r}
        </div>
      ))}
    </div>
  );
}

function originTitle(cause: RootCause): string {
  return cause.explanation_source === "claude"
    ? "Written by Claude from the call chain and the code around the offending line"
    : "Built from the finding without a model: Claude was unavailable or declined to explain this one";
}

function FindingCard({ finding }: { finding: Finding }) {
  const color = SEVERITY_COLORS[finding.severity];
  return (
    <article className="finding" style={{ "--badge-color": color } as CSSProperties}>
      <div className="finding__head">
        <span className="badge">{finding.severity}</span>
        <span className="finding__title">{finding.title}</span>
      </div>
      <div className="finding__tags">
        <span className="tag">{CATEGORY_LABELS[finding.category]}</span>
        <span className="tag tag--mono">{finding.rule_id}</span>
        <span className="tag">{SOURCE_LABELS[finding.source]}</span>
        {finding.owasp && (
          <span className="tag tag--owasp" title={`OWASP Top 10 ${finding.owasp}`}>
            {finding.owasp.split(":")[0]} · {finding.owasp_name}
          </span>
        )}
        {finding.seen_before > 0 && (
          <span
            className="tag tag--seen"
            title={`This pattern (signature ${finding.signature}) was recorded in ${finding.seen_before} earlier run${finding.seen_before === 1 ? "" : "s"}`}
          >
            <HistoryIcon size={11} /> Seen before ×{finding.seen_before}
            {finding.base_severity && <> · raised from {finding.base_severity}</>}
          </span>
        )}
      </div>
      <p className="finding__evidence">{finding.evidence}</p>
      {finding.exception && (
        <div className="finding__exception">
          <span className="finding__exception-type">{finding.exception.type}</span>
          {finding.exception.message}
        </div>
      )}
      <div className="finding__where">
        {finding.location.file}:{finding.location.line}
        {finding.source_test && <span className="finding__test"> · surfaced by {finding.source_test}</span>}
      </div>
    </article>
  );
}

function Breadcrumb({ chain }: { chain: ChainStep[] }) {
  return (
    <ol className="crumbs" aria-label="Causal chain from the entry point to the offending line">
      {chain.map((step, i) => (
        <Fragment key={`${step.node_id}:${i}`}>
          {i > 0 && (
            <li className="crumbs__sep" aria-hidden>
              <ChevronRight size={14} />
            </li>
          )}
          <li
            className={`crumb crumb--${step.role}`}
            title={step.file ? `${step.file}${step.line ? `:${step.line}` : ""}${step.via === "traceback" ? " (from the traceback)" : ""}` : step.label}
          >
            <span className="crumb__label">{step.label}</span>
            {step.file && step.line && (
              <span className="crumb__where">
                {step.file.split("/").pop()}:{step.line}
              </span>
            )}
            {step.role !== "path" && <span className="crumb__role">{step.role === "entry" ? "entry" : "offending line"}</span>}
          </li>
        </Fragment>
      ))}
    </ol>
  );
}

function CodeBlock({ snippet }: { snippet: CodeSnippet }) {
  const lines = snippet.code.split("\n");
  const width = String(snippet.start_line + lines.length - 1).length;
  return (
    <pre className="code" tabIndex={0} aria-label={`Code from ${snippet.file}`}>
      {lines.map((text, i) => {
        const number = snippet.start_line + i;
        const hot = number === snippet.highlight_line;
        return (
          <div key={number} className={hot ? "code__line code__line--hot" : "code__line"} aria-current={hot ? "location" : undefined}>
            <span className="code__number" style={{ minWidth: `${width}ch` }}>
              {number}
            </span>
            <span className="code__text">
              {text === "" ? "​" : tokenizeLine(text).map((t, j) => (t.kind === "plain" ? t.text : <span key={j} className={`tok tok--${t.kind}`}>{t.text}</span>))}
            </span>
          </div>
        );
      })}
    </pre>
  );
}
