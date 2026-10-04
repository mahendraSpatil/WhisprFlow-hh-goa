import { DiffEditor } from "@monaco-editor/react";
import { CheckCircle2, ChevronsDownUp, Copy, Lightbulb, Loader2, ShieldCheck, ShieldX, X } from "lucide-react";
import type { editor as MonacoEditor } from "monaco-editor/editor/editor.api"; // types only: erased at build time
import { useEffect, useRef, useState } from "react";

import type { Patch, PatchVerdict } from "../api/types";
import { THEME } from "../monaco"; // importing it also bundles and configures Monaco
import { diffSummary, languageFor } from "../patches";
import { verifiedSummary } from "../regression";

interface PatchViewProps {
  patch: Patch;
  verdict: PatchVerdict | undefined;
  title: string;
  onClose: () => void;
}

/** Scroll both sides to the first change once Monaco has computed the diff (it opens at line 1 otherwise). */
function revealFirstChange(diff: MonacoEditor.IStandaloneDiffEditor) {
  const reveal = () => {
    const first = diff.getLineChanges()?.[0];
    if (!first) return false;
    const line = first.modifiedStartLineNumber || first.originalStartLineNumber;
    diff.getModifiedEditor().revealLineNearTop(Math.max(1, line - 4));
    return true;
  };
  if (reveal()) return;
  const subscription = diff.onDidUpdateDiff(() => {
    if (reveal()) subscription.dispose();
  });
}

/** Full-width side-by-side review of a patch: the design suggestion on top, original and patched file below. */
export default function PatchView({ patch, verdict, title, onClose }: PatchViewProps) {
  const [active, setActive] = useState(0);
  const [copied, setCopied] = useState(false);
  const [collapse, setCollapse] = useState(false);
  const closeButton = useRef<HTMLButtonElement>(null);
  const file = patch.files[Math.min(active, patch.files.length - 1)];

  useEffect(() => {
    closeButton.current?.focus();
    // Capture phase, so Esc closes only this view and not the findings drawer underneath it.
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  async function copyDiff() {
    try {
      await navigator.clipboard.writeText(patch.diff);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable (insecure context or denied); nothing useful to do
    }
  }

  return (
    <div className="patchview" role="dialog" aria-modal="true" aria-label={`Patch for ${title}`}>
      <header className="patchview__header">
        <div className="patchview__heading">
          <div className="patchview__eyebrow">Patch</div>
          <h2 className="patchview__title">{title}</h2>
          <div className="patchview__path">{file.path}</div>
        </div>
        <span className="patchview__status" title="Checked with git apply --check, and the patched files still parse">
          <CheckCircle2 size={14} /> {diffSummary(patch)}
        </span>
        {verdict && (
          <span
            className={verdict.accepted ? "patchview__status patchview__status--verified" : "patchview__status patchview__status--rejected"}
            title={verdict.reasons.join("\n") || "Verified on a fresh copy of the repo"}
          >
            {verdict.accepted ? <ShieldCheck size={14} /> : <ShieldX size={14} />}
            {verdict.accepted ? `Verified: ${verifiedSummary(verdict)}` : `Rejected: ${verdict.reasons[0] ?? verdict.verdict}`}
          </span>
        )}
        <button
          className={collapse ? "btn btn--ghost is-on" : "btn btn--ghost"}
          onClick={() => setCollapse((on) => !on)}
          aria-pressed={collapse}
          title="Fold the lines that did not change, leaving a few lines of context around each change"
        >
          <ChevronsDownUp size={14} /> Collapse unchanged
        </button>
        <button className="btn btn--ghost" onClick={copyDiff}>
          <Copy size={14} /> {copied ? "Copied" : "Copy diff"}
        </button>
        <button ref={closeButton} className="icon-btn" onClick={onClose} aria-label="Close patch" title="Close (Esc)">
          <X size={18} />
        </button>
      </header>

      <section className="patchview__design">
        <h3 className="patchview__label">
          <Lightbulb size={13} /> Hardened design
        </h3>
        <p className="patchview__text">
          {patch.design_suggestion || "Claude did not include a design suggestion for this fix."}
        </p>
      </section>

      {patch.files.length > 1 && (
        <div className="patchview__tabs" role="tablist">
          {patch.files.map((f, i) => (
            <button
              key={f.path}
              role="tab"
              aria-selected={i === active}
              className={i === active ? "patchview__tab is-active" : "patchview__tab"}
              onClick={() => setActive(i)}
            >
              {f.path}
            </button>
          ))}
        </div>
      )}

      <div className="patchview__columns" aria-hidden>
        <span>Original</span>
        <span>Patched</span>
      </div>

      <div className="patchview__editor">
        <DiffEditor
          key={file.path}
          original={file.original}
          modified={file.patched}
          language={languageFor(file.path)}
          theme={THEME}
          onMount={revealFirstChange}
          loading={
            <div className="patchview__loading">
              <Loader2 size={18} className="spin" /> Loading editor…
            </div>
          }
          options={{
            readOnly: true,
            originalEditable: false,
            renderSideBySide: true,
            enableSplitViewResizing: false,
            automaticLayout: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            renderOverviewRuler: false,
            renderLineHighlight: "none",
            hideUnchangedRegions: { enabled: collapse, contextLineCount: 4, minimumLineCount: 4, revealLineCount: 20 },
            fontFamily: "JetBrains Mono, Cascadia Code, SF Mono, ui-monospace, Consolas, monospace",
            fontSize: 13,
            lineHeight: 21,
            padding: { top: 10, bottom: 10 },
            diffAlgorithm: "advanced",
            ignoreTrimWhitespace: false,
          }}
        />
      </div>
    </div>
  );
}
