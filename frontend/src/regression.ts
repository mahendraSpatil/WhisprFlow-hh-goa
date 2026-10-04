import type {
  Diagnostics,
  Patch,
  PatchVerdict,
  RegressionCheckOutput,
  RegressionSummary,
  SuiteTotals,
} from "./api/types";

export function verdictFor(regression: RegressionCheckOutput | null, patchId: string): PatchVerdict | undefined {
  return regression?.verdicts.find((v) => v.patch_id === patchId);
}

/** Tests that are failing or erroring. */
export const failing = (t: SuiteTotals): number => t.failed + t.errors;

export type Tone = "good" | "bad" | "same";

/** A before/after pair, with whether the change is an improvement ("good") or a regression ("bad"). */
export interface Delta {
  label: string;
  before: number;
  after: number;
  tone: Tone;
}

function delta(label: string, before: number, after: number, lowerIsBetter: boolean): Delta {
  const better = lowerIsBetter ? after < before : after > before;
  const worse = lowerIsBetter ? after > before : after < before;
  return { label, before, after, tone: better ? "good" : worse ? "bad" : "same" };
}

export function summaryDeltas(s: RegressionSummary): Delta[] {
  return [
    delta("Tests passing", s.tests_before.passed, s.tests_after.passed, false),
    delta("Tests failing", failing(s.tests_before), failing(s.tests_after), true),
    delta("Findings", s.findings_before, s.findings_after, true),
  ];
}

export function verdictCounts(regression: RegressionCheckOutput): { accepted: number; rejected: number } {
  const accepted = regression.verdicts.filter((v) => v.accepted).length;
  return { accepted, rejected: regression.verdicts.length - accepted };
}

/** A readable name for a patch: the function it patches, else the file and line of the root cause. */
export function patchLabel(patchId: string, patches: Patch[] | null, diagnostics: Diagnostics | null): string {
  const patch = patches?.find((p) => p.id === patchId);
  const cause = diagnostics?.root_causes.find((c) => c.id === patch?.root_cause_id);
  if (!cause) return patchId;
  const name = cause.symbol ? cause.symbol.split(".").slice(-2).join(".") : null;
  return name ?? `${cause.location.file}:${cause.location.line}`;
}

/** One line on what a verified patch did, e.g. "fixes 3 tests, resolves 2 findings, breaks nothing". */
export function verifiedSummary(v: PatchVerdict): string {
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const parts: string[] = [];
  if (v.newly_passing.length) parts.push(`fixes ${plural(v.newly_passing.length, "test")}`);
  if (v.findings_resolved.length) parts.push(`resolves ${plural(v.findings_resolved.length, "finding")}`);
  if (!parts.length) parts.push("changes no test or finding");
  return `${parts.join(", ")}, breaks nothing`;
}
