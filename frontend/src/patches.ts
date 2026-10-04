import type { Patch } from "./api/types";

export function patchForCause(patches: Patch[] | null, causeId: string): Patch | undefined {
  return patches?.find((p) => p.root_cause_id === causeId);
}

const LANGUAGES: Record<string, string> = {
  py: "python",
  pyi: "python",
  md: "markdown",
  json: "json",
  toml: "ini",
  yaml: "yaml",
  yml: "yaml",
  txt: "plaintext",
  cfg: "ini",
  ini: "ini",
  sh: "shell",
  sql: "sql",
};

/** Monaco language id for a file path; plain text when unknown. */
export function languageFor(path: string): string {
  const name = path.split("/").pop() ?? path;
  const extension = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return LANGUAGES[extension] ?? "plaintext";
}

/** The unified diff as Claude returned it, for copying out of the UI. */
export function diffSummary(patch: Patch): string {
  const files = patch.files.length;
  const retried = patch.attempts > 1 ? ", after one retry" : "";
  return `${files} file${files === 1 ? "" : "s"}, applies cleanly${retried}`;
}
