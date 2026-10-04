import { describe, expect, it } from "vitest";

import { ApiError } from "./api/client";
import type { PullRequestResult, PullRequestStatus } from "./api/types";
import { branchLine, prView, problemFrom, safeHttpUrl, type PrInputs } from "./pullRequest";

const status = (over: Partial<PullRequestStatus> = {}): PullRequestStatus => ({
  ready: true, blocker: null, repo: "acme/shop", branch: "codeloop/fix-abc", base: "main", title: "Fix things",
  patch_ids: ["p-1"], pull_request: null, ...over,
});
const result = (over: Partial<PullRequestResult> = {}): PullRequestResult => ({
  number: 7, url: "https://github.com/acme/shop/pull/7", repo: "acme/shop", branch: "codeloop/fix-abc", base: "main",
  title: "Fix things", commits: 2, draft: false, created: true, notes: [], ...over,
});
const inputs = (over: Partial<PrInputs> = {}): PrInputs => ({ status: status(), result: null, creating: false, problem: null, loaded: true, ...over });

describe("pull request view", () => {
  it("loads, then offers the button", () => {
    expect(prView(inputs({ status: null, loaded: false })).kind).toBe("loading");
    expect(prView(inputs()).kind).toBe("ready");
  });

  it("explains a blocker instead of offering the button", () => {
    const blocker = { code: "github_token_missing", message: "GITHUB_TOKEN is not set" };
    expect(prView(inputs({ status: status({ ready: false, blocker }) }))).toEqual({ kind: "blocked", blocker });
  });

  it("shows progress while creating", () => {
    expect(prView(inputs({ creating: true })).kind).toBe("creating");
  });

  it("shows the link when done, from the response or from the status of an earlier click", () => {
    const view = prView(inputs({ result: result() }));
    expect(view).toMatchObject({ kind: "done", url: "https://github.com/acme/shop/pull/7", headline: "Pull request #7 opened" });
    expect(prView(inputs({ status: status({ pull_request: result({ created: false, draft: true }) }) }))).toMatchObject({
      kind: "done", headline: "Draft pull request #7 already open",
    });
    // a finished pull request wins over a stale error or an in-flight flag
    expect(prView(inputs({ result: result(), creating: true, problem: { code: null, message: "x" } })).kind).toBe("done");
  });

  it("never links to a non-http url", () => {
    expect(prView(inputs({ result: result({ url: "javascript:alert(1)" }) }))).toMatchObject({ kind: "done", url: null });
    expect(safeHttpUrl("https://github.com/a/b/pull/1")).toBe("https://github.com/a/b/pull/1");
    expect(safeHttpUrl("data:text/html,x")).toBeNull();
    expect(safeHttpUrl("not a url")).toBeNull();
  });

  it("lets the user retry errors that a retry can fix", () => {
    expect(prView(inputs({ problem: { code: "github_rate_limited", message: "slow down" } }))).toMatchObject({ kind: "error", retryable: true });
    expect(prView(inputs({ problem: { code: null, message: "network" } }))).toMatchObject({ kind: "error", retryable: true });
    expect(prView(inputs({ problem: { code: "github_token_missing", message: "no token" } }))).toMatchObject({ kind: "error", retryable: false });
  });

  it("reads the backend's error code and message", () => {
    const err = new ApiError(400, "GITHUB_TOKEN is not set", { code: "github_token_missing", message: "GITHUB_TOKEN is not set" });
    expect(problemFrom(err)).toEqual({ code: "github_token_missing", message: "GITHUB_TOKEN is not set" });
    expect(problemFrom(new ApiError(0, "Cannot reach the CodeLoop backend."))).toEqual({ code: null, message: "Cannot reach the CodeLoop backend." });
    expect(problemFrom(new Error("boom"))).toEqual({ code: null, message: "boom" });
    expect(problemFrom("odd").message).toMatch(/something went wrong/i);
  });

  it("names the branch", () => {
    expect(branchLine({ branch: "codeloop/fix-abc", base: "main" })).toBe("codeloop/fix-abc → main");
    expect(branchLine({ branch: "codeloop/fix-abc", base: null })).toBe("codeloop/fix-abc");
  });
});
