import { ArrowRight, CheckCircle2, ChevronDown, ShieldCheck, XCircle } from "lucide-react";

import type { PatchVerdict } from "../api/types";
import { patchLabel, summaryDeltas, verdictCounts } from "../regression";
import type { RunState } from "../state/runReducer";
import { PullRequestAction } from "./PullRequestAction";

/** Before and after for the accepted patches, plus the patches that were rejected and why. Shown under RegressionCheck. */
export function RegressionCard({ run }: { run: RunState }) {
  const { regression } = run;
  if (!regression) return null;
  const { summary } = regression;
  if (!summary) {
    // Say why, from the first patch that was not produced (e.g. no API key, or a diff that would not apply).
    const reason = run.patches?.find((p) => p.detail)?.detail;
    return (
      <div className="rcard rcard--empty">
        <div className="rcard__empty-title">Nothing to verify</div>
        No patch was valid{run.patches?.length === 0 ? " because no issues were found" : ""}.
        {reason && <div className="rcard__empty-reason">{reason}</div>}
      </div>
    );
  }

  const { accepted, rejected } = verdictCounts(regression);
  const rejectedVerdicts = regression.verdicts.filter((v) => !v.accepted);
  const label = (v: PatchVerdict) => patchLabel(v.patch_id, run.patches, run.diagnostics);

  return (
    <div className="rcard" aria-label="Before and after verification">
      <div className="rcard__head" title="Each patch was applied to a fresh copy of the repo, then the tests and the scans were re-run">
        <ShieldCheck size={13} /> Verified
        <span className="rcard__patches">
          {accepted} accepted{rejected > 0 && <span className="rcard__rejected"> · {rejected} rejected</span>}
        </span>
      </div>

      <div className="rcard__cols" aria-hidden>
        <span />
        <span>Before</span>
        <span />
        <span>After</span>
      </div>
      {summaryDeltas(summary).map((d) => (
        <div key={d.label} className={`rcard__row rcard__row--${d.tone}`}>
          <span className="rcard__label">{d.label}</span>
          <span className="rcard__num">{d.before}</span>
          <ArrowRight size={12} className="rcard__arrow" aria-hidden />
          <span className="rcard__num rcard__num--after">{d.after}</span>
        </div>
      ))}

      <div className="rcard__chips">
        <span className={summary.tests_fixed.length ? "chip chip--good" : "chip"}>{summary.tests_fixed.length} tests fixed</span>
        <span className={summary.tests_broken.length ? "chip chip--bad" : "chip"}>{summary.tests_broken.length} newly broken</span>
        <span className={summary.findings_resolved.length ? "chip chip--good" : "chip"}>
          {summary.findings_resolved.length} findings resolved
        </span>
        {summary.findings_introduced.length > 0 && <span className="chip chip--bad">{summary.findings_introduced.length} new findings</span>}
      </div>

      {summary.tests_fixed.length > 0 && (
        <details className="rcard__details">
          <summary>
            <ChevronDown size={12} /> Tests fixed
          </summary>
          <ul>
            {summary.tests_fixed.map((t) => (
              <li key={t} title={t}>
                <CheckCircle2 size={11} /> {t.split("::").pop()}
              </li>
            ))}
          </ul>
        </details>
      )}

      {rejectedVerdicts.length > 0 && (
        <div className="rcard__rejections">
          {rejectedVerdicts.map((v) => (
            <div key={v.patch_id} className="rcard__rejection" title={v.reasons.join("\n")}>
              <div className="rcard__rejection-name">
                <XCircle size={12} /> {label(v)}
              </div>
              <div className="rcard__rejection-reason">{v.reasons[0] ?? "rejected"}</div>
            </div>
          ))}
        </div>
      )}

      {accepted > 0 && run.runId && <PullRequestAction key={run.runId} runId={run.runId} />}
    </div>
  );
}
