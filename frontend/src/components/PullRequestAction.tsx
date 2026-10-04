import { ExternalLink, GitPullRequest, KeyRound, Loader2, RefreshCw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { createPullRequest, getPullRequestStatus } from "../api/client";
import type { PullRequestResult, PullRequestStatus } from "../api/types";
import { branchLine, prView, problemFrom, type PrProblem } from "../pullRequest";

function usePullRequest(runId: string) {
  const [status, setStatus] = useState<PullRequestStatus | null>(null);
  const [result, setResult] = useState<PullRequestResult | null>(null);
  const [creating, setCreating] = useState(false);
  const [problem, setProblem] = useState<PrProblem | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    setProblem(null);
    try {
      const next = await getPullRequestStatus(runId);
      if (alive.current) setStatus(next);
    } catch (err) {
      if (alive.current) setProblem(problemFrom(err));
    } finally {
      if (alive.current) setLoaded(true);
    }
  }, [runId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const create = useCallback(async () => {
    setCreating(true);
    setProblem(null);
    try {
      const made = await createPullRequest(runId, draft);
      if (alive.current) setResult(made);
    } catch (err) {
      if (alive.current) setProblem(problemFrom(err));
    } finally {
      if (alive.current) setCreating(false);
    }
  }, [runId, draft]);

  return { status, result, creating, problem, loaded, draft, setDraft, refresh, create };
}

/** "Create Pull Request" under the regression summary: branch, one commit per accepted patch, and the PR link. */
export function PullRequestAction({ runId }: { runId: string }) {
  const pr = usePullRequest(runId);
  const view = prView(pr);

  if (view.kind === "done") {
    const { result } = view;
    const label = (
      <>
        <GitPullRequest size={14} />
        <span className="pr__headline">{view.headline}</span>
        {view.url && <ExternalLink size={12} aria-hidden />}
      </>
    );
    return (
      <div className="pr pr--done">
        {view.url ? (
          <a className="pr__link" href={view.url} target="_blank" rel="noopener noreferrer">
            {label}
          </a>
        ) : (
          <div className="pr__link">{label}</div>
        )}
        <div className="pr__meta">
          {result.repo} · {branchLine(result)} · {result.commits} commit{result.commits === 1 ? "" : "s"}
        </div>
        {result.notes.map((note) => (
          <div key={note} className="pr__note">
            {note}
          </div>
        ))}
      </div>
    );
  }

  const busy = view.kind === "creating" || view.kind === "loading";
  const canCreate = view.kind === "ready" || (view.kind === "error" && view.retryable && pr.status !== null);

  return (
    <div className="pr">
      <div className="pr__row">
        <button
          type="button"
          className="btn btn--primary btn--sm"
          disabled={!canCreate || busy}
          onClick={() => void pr.create()}
          title="Create the branch, commit the accepted patches and open a pull request on GitHub"
        >
          {view.kind === "creating" ? <Loader2 size={13} className="spin" /> : <GitPullRequest size={13} />}
          {view.kind === "creating" ? "Creating pull request…" : "Create Pull Request"}
        </button>
        <label className="pr__draft">
          <input type="checkbox" checked={pr.draft} disabled={view.kind === "creating"} onChange={(e) => pr.setDraft(e.target.checked)} /> Open as
          draft
        </label>
      </div>

      {view.kind === "ready" && (
        <div className="pr__meta" title={view.status.title}>
          {view.status.repo} · {branchLine(view.status)} · {view.status.patch_ids.length} commit
          {view.status.patch_ids.length === 1 ? "" : "s"}
        </div>
      )}

      {view.kind === "creating" && (
        <div className="pr__meta" role="status">
          Committing the patches to {pr.status?.branch ?? "a new branch"}. GitHub calls are paced, so this can take several seconds.
        </div>
      )}

      {view.kind === "blocked" && (
        <div className="pr__problem pr__problem--blocked" role="status">
          {view.blocker.code === "github_token_missing" ? <KeyRound size={13} /> : <TriangleAlert size={13} />}
          <div>
            <div>{view.blocker.message}</div>
            <button type="button" className="pr__again" onClick={() => void pr.refresh()}>
              <RefreshCw size={11} /> Check again
            </button>
          </div>
        </div>
      )}

      {view.kind === "error" && (
        <div className="pr__problem" role="alert">
          <TriangleAlert size={13} />
          <div>
            <div>{view.problem.message}</div>
            <button type="button" className="pr__again" onClick={() => void (pr.status === null || !view.retryable ? pr.refresh() : pr.create())}>
              <RefreshCw size={11} /> {pr.status === null || !view.retryable ? "Check again" : "Try again"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
