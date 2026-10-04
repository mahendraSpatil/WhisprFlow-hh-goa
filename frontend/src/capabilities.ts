import type { Environment } from "./api/types";

export interface Capability {
  key: "ai" | "github" | "memory";
  label: string;
  on: boolean;
  /** What it does when on, or how to switch it on. */
  hint: string;
}

/** The switches the landing page shows: what this server can do, and how to enable what it cannot. */
export function capabilities(env: Environment): Capability[] {
  return [
    {
      key: "ai",
      label: "AI patches",
      on: env.ai,
      hint: env.ai
        ? "Claude explains root causes and writes the patches."
        : "Set ANTHROPIC_API_KEY to get Claude's explanations and patches. Without it, findings and root causes still work.",
    },
    {
      key: "github",
      label: "GitHub pull requests",
      on: env.github,
      hint: env.github ? "Verified fixes can be opened as a pull request." : "Set GITHUB_TOKEN to open verified fixes as a pull request.",
    },
    {
      key: "memory",
      label: "Incident memory",
      on: env.memory,
      hint: env.memory ? "Repeated patterns are flagged and past fixes guide new patches." : "CODELOOP_MEMORY=off: nothing is remembered between runs.",
    },
  ];
}
