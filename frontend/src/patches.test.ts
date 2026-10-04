import { describe, expect, it } from "vitest";

import type { Patch } from "./api/types";
import { diffSummary, languageFor, patchForCause } from "./patches";

const patch = (over: Partial<Patch> = {}): Patch => ({
  id: "P-1",
  root_cause_id: "RC-1",
  status: "valid",
  detail: null,
  diff: "",
  files: [{ path: "orders/db.py", original: "a", patched: "b" }],
  design_suggestion: "",
  attempts: 1,
  rationale: "",
  memory_examples: 0,
  ...over,
});

describe("patch helpers", () => {
  it("finds the patch for a root cause", () => {
    const patches = [patch({ id: "P-1", root_cause_id: "RC-1" }), patch({ id: "P-2", root_cause_id: "RC-2" })];
    expect(patchForCause(patches, "RC-2")?.id).toBe("P-2");
    expect(patchForCause(patches, "RC-9")).toBeUndefined();
    expect(patchForCause(null, "RC-1")).toBeUndefined(); // not loaded yet
  });

  it("picks the Monaco language from the file extension", () => {
    expect(languageFor("orders/db.py")).toBe("python");
    expect(languageFor("docs/README.MD")).toBe("markdown");
    expect(languageFor("pyproject.toml")).toBe("ini");
    expect(languageFor("Makefile")).toBe("plaintext");
    expect(languageFor("weird.name.unknown")).toBe("plaintext");
  });

  it("summarizes a patch, mentioning a retry", () => {
    expect(diffSummary(patch())).toBe("1 file, applies cleanly");
    expect(diffSummary(patch({ attempts: 2 }))).toBe("1 file, applies cleanly, after one retry");
    const two = patch({ files: [...patch().files, { path: "b.py", original: "", patched: "" }] });
    expect(diffSummary(two)).toBe("2 files, applies cleanly");
  });
});
