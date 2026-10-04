import { describe, expect, it } from "vitest";

import type { Diagnostics, Patch, PatchVerdict, RegressionCheckOutput, RegressionSummary, RootCause } from "./api/types";
import { patchLabel, summaryDeltas, verdictCounts, verdictFor, verifiedSummary } from "./regression";

const totals = (passed: number, failed: number, errors = 0) => ({ passed, failed, errors, skipped: 0 });
const where = { file: "orders/db.py", line: 53, end_line: null, column: null };

const verdict = (over: Partial<PatchVerdict> = {}): PatchVerdict => ({
  patch_id: "P-1", root_cause_id: "RC-1", verdict: "pass", accepted: true, reasons: [], totals: totals(24, 0),
  newly_failing: [], newly_passing: [], findings_resolved: [], findings_introduced: [], detail: null, ...over,
});

const summary = (over: Partial<RegressionSummary> = {}): RegressionSummary => ({
  patches_applied: ["P-1"], tests_before: totals(17, 7), tests_after: totals(24, 0), tests_fixed: [], tests_broken: [],
  findings_before: 7, findings_after: 0, findings_resolved: [], findings_introduced: [], ...over,
});

describe("regression helpers", () => {
  it("colors each before/after pair by whether it improved", () => {
    const [passing, failing, findings] = summaryDeltas(summary());
    expect([passing, failing, findings].map((d) => [d.label, d.before, d.after, d.tone])).toEqual([
      ["Tests passing", 17, 24, "good"],
      ["Tests failing", 7, 0, "good"],
      ["Findings", 7, 0, "good"],
    ]);

    const worse = summaryDeltas(summary({ tests_after: totals(15, 2, 1), findings_after: 9 }));
    expect(worse.map((d) => d.tone)).toEqual(["bad", "good", "bad"]); // passing fell, failing fell 7 -> 3, findings rose
    expect(worse[1]).toMatchObject({ before: 7, after: 3 }); // errors count as failing

    expect(summaryDeltas(summary({ tests_after: totals(17, 7), findings_after: 7 })).map((d) => d.tone)).toEqual(["same", "same", "same"]);
  });

  it("looks verdicts up and counts accepted and rejected", () => {
    const regression: RegressionCheckOutput = {
      baseline_totals: null,
      summary: null,
      verdicts: [verdict({ patch_id: "A" }), verdict({ patch_id: "B", accepted: false, verdict: "regressed" }), verdict({ patch_id: "C" })],
    };
    expect(verdictFor(regression, "B")?.verdict).toBe("regressed");
    expect(verdictFor(regression, "Z")).toBeUndefined();
    expect(verdictFor(null, "A")).toBeUndefined();
    expect(verdictCounts(regression)).toEqual({ accepted: 2, rejected: 1 });
  });

  it("describes what a verified patch did", () => {
    const ref = { finding_id: "F", rule_id: "B608", title: "t", severity: "medium" as const, location: where, node_id: null };
    expect(verifiedSummary(verdict({ newly_passing: ["a", "b", "c"], findings_resolved: [ref, ref] }))).toBe(
      "fixes 3 tests, resolves 2 findings, breaks nothing",
    );
    expect(verifiedSummary(verdict({ newly_passing: ["a"] }))).toBe("fixes 1 test, breaks nothing");
    expect(verifiedSummary(verdict())).toBe("changes no test or finding, breaks nothing");
  });

  it("names a patch after the function it patches", () => {
    const cause = (symbol: string | null): RootCause => ({
      id: "RC-1", finding_ids: ["F"], location: where, node_id: null, symbol, chain: [], snippet: null,
      explanation: "", explanation_source: "template", confidence: 0.5, evidence: [],
    });
    const patches = [{ id: "P-1", root_cause_id: "RC-1" } as Patch];
    const diagnostics = (symbol: string | null) => ({ root_causes: [cause(symbol)] }) as unknown as Diagnostics;

    expect(patchLabel("P-1", patches, diagnostics("orders.service.Inventory.reserve"))).toBe("Inventory.reserve");
    expect(patchLabel("P-1", patches, diagnostics(null))).toBe("orders/db.py:53");
    expect(patchLabel("P-9", patches, diagnostics("x.y"))).toBe("P-9"); // unknown patch: fall back to its id
    expect(patchLabel("P-1", null, null)).toBe("P-1");
  });
});
