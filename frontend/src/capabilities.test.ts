import { describe, expect, it } from "vitest";

import { capabilities } from "./capabilities";
import { PHASES, STAGES, STAGE_DETAILS } from "./stages";

describe("capabilities", () => {
  it("says how to switch on what is off", () => {
    const [ai, github, memory] = capabilities({ demo_path: null, ai: false, github: false, memory: true });
    expect([ai!.on, github!.on, memory!.on]).toEqual([false, false, true]);
    expect(ai!.hint).toContain("ANTHROPIC_API_KEY");
    expect(github!.hint).toContain("GITHUB_TOKEN");
  });

  it("describes what is on", () => {
    const all = capabilities({ demo_path: "/x", ai: true, github: true, memory: true });
    expect(all.every((c) => c.on)).toBe(true);
    expect(all.map((c) => c.key)).toEqual(["ai", "github", "memory"]);
  });
});

describe("the nine stages on the landing page", () => {
  it("are grouped into three acts that cover every stage once, in order", () => {
    expect(PHASES).toHaveLength(3);
    expect(PHASES.flatMap((p) => p.stages)).toEqual(STAGES.map((s) => s.name));
  });

  it("each have a description", () => {
    for (const s of STAGES) expect(STAGE_DETAILS[s.name].length).toBeGreaterThan(30);
  });
});
