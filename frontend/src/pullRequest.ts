import { ApiError } from "./api/client";
import type { PullRequestBlocker, PullRequestResult, PullRequestStatus } from "./api/types";

/** The URL if it is http(s), else null: a link from a server response must never become a javascript: URL. */
export function safeHttpUrl(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export interface PrProblem {
  /** The backend's stable error code (e.g. "branch_exists"), or null for a network or unexpected failure. */
  code: string | null;
  message: string;
}

/** What went wrong, from an ApiError whose detail is {code, message} or from anything else. */
export function problemFrom(err: unknown): PrProblem {
  if (err instanceof ApiError) {
    const detail = err.detail;
    const code = detail && typeof detail === "object" && "code" in detail ? String((detail as { code: unknown }).code) : null;
    return { code, message: err.message };
  }
  return { code: null, message: err instanceof Error ? err.message : "Something went wrong creating the pull request." };
}

export type PrView =
  | { kind: "loading" }
  | { kind: "ready"; status: PullRequestStatus }
  | { kind: "blocked"; blocker: PullRequestBlocker }
  | { kind: "creating" }
  | { kind: "done"; result: PullRequestResult; url: string | null; headline: string }
  | { kind: "error"; problem: PrProblem; retryable: boolean };

export interface PrInputs {
  status: PullRequestStatus | null;
  result: PullRequestResult | null;
  creating: boolean;
  problem: PrProblem | null;
  /** Whether the first status request has finished (successfully or not). */
  loaded: boolean;
}

/** Errors that creating again cannot fix: the user has to change something first. */
const NOT_RETRYABLE = new Set(["github_token_missing", "github_repo_unknown", "no_accepted_patches", "regression_not_ready"]);

export function headline(result: PullRequestResult): string {
  const kind = result.draft ? "Draft pull request" : "Pull request";
  return `${kind} #${result.number} ${result.created ? "opened" : "already open"}`;
}

/** Which of the card's states to show. A pull request that exists beats everything; a blocker beats a ready button. */
export function prView({ status, result, creating, problem, loaded }: PrInputs): PrView {
  const existing = result ?? status?.pull_request ?? null;
  if (existing) return { kind: "done", result: existing, url: safeHttpUrl(existing.url), headline: headline(existing) };
  if (creating) return { kind: "creating" };
  if (problem) return { kind: "error", problem, retryable: !(problem.code && NOT_RETRYABLE.has(problem.code)) };
  if (!loaded || !status) return { kind: "loading" };
  if (status.blocker) return { kind: "blocked", blocker: status.blocker };
  return { kind: "ready", status };
}

/** "codeloop/fix-ab12 → main", for under the button. */
export function branchLine(status: Pick<PullRequestStatus, "branch" | "base">): string {
  return status.base ? `${status.branch} → ${status.base}` : status.branch;
}
