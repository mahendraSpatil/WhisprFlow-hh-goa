import type {
  CreateRunResponse,
  Diagnostics,
  Environment,
  Graph,
  Incidents,
  Patch,
  PullRequestResult,
  PullRequestStatus,
  RegressionCheckOutput,
  RunSummary,
} from "./types";

const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

function describe(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI request-validation errors: [{loc, msg, type}, ...]
    return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
  }
  if (detail && typeof detail === "object" && "message" in detail) return String(detail.message);
  return fallback;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the CodeLoop backend. Is it running on port 8000?");
  }
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = (await response.json()).detail;
    } catch {
      // non-JSON error body
    }
    throw new ApiError(response.status, describe(detail, `${response.status} ${response.statusText}`), detail);
  }
  return response.json() as Promise<T>;
}

export const createRun = (source: string) =>
  request<CreateRunResponse>("/runs", { method: "POST", body: JSON.stringify({ source }) });

export const getRun = (runId: string) => request<RunSummary>(`/runs/${encodeURIComponent(runId)}`);

export const getGraph = (runId: string) => request<Graph>(`/runs/${encodeURIComponent(runId)}/graph`);

export const getDiagnostics = (runId: string) =>
  request<Diagnostics>(`/runs/${encodeURIComponent(runId)}/diagnostics`);

export const getIncidents = (limit = 500) => request<Incidents>(`/memory/incidents?limit=${limit}`);

export const getRegression = (runId: string) =>
  request<RegressionCheckOutput>(`/runs/${encodeURIComponent(runId)}/regression`);

export const getPatches = (runId: string) =>
  request<{ patches: Patch[] }>(`/runs/${encodeURIComponent(runId)}/patches`).then((r) => r.patches);

export const getEnvironment = () => request<Environment>("/environment");

export const getPullRequestStatus = (runId: string) =>
  request<PullRequestStatus>(`/runs/${encodeURIComponent(runId)}/pull-request`);

/** Branch codeloop/fix-{runId}, one commit per accepted patch, then the pull request. Slow: GitHub calls are paced. */
export const createPullRequest = (runId: string, draft: boolean) =>
  request<PullRequestResult>(`/runs/${encodeURIComponent(runId)}/pull-request`, {
    method: "POST",
    body: JSON.stringify({ draft }),
  });

/** WebSocket URL for a run's events; ``since`` resumes after the last event already seen. */
export function eventsUrl(runId: string, since: number): string {
  const url = new URL(API_BASE, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/$/, "")}/ws/runs/${encodeURIComponent(runId)}`;
  url.search = since > 0 ? `?since=${since}` : "";
  return url.toString();
}
