import { describe, expect, it } from "vitest";

import type { Incident } from "./api/types";
import { diffLineKind, filterIncidents, historyStats, outcomeOf, relativeTime } from "./history";

let next = 1;
const incident = (over: Partial<Incident> = {}): Incident => ({
  id: next++, signature: "sig-a", run_id: "run-1", source: "/repo/shop", created_at: "2026-10-04T12:00:00+00:00", kind: "B608",
  pattern: "_ = fstr(_)", owasp: "A03:2021", category: "security", severity: "medium", rule_id: "B608",
  title: "Possible SQL injection vector", file: "orders/db.py", line: 53, symbol: "orders.db.find_orders_by_customer",
  root_cause: "String-built SQL.", patch_status: "none", patch_diff: null, patch_design: null, regression_passed: null,
  regression_reasons: [], occurrences: 1, ...over,
});

describe("history helpers", () => {
  it("says what became of an incident", () => {
    expect(outcomeOf(incident({ regression_passed: true, patch_status: "valid" }))).toMatchObject({ label: "Fix verified", tone: "good" });
    expect(outcomeOf(incident({ regression_passed: false, patch_status: "valid", regression_reasons: ["breaks 2 tests"] }))).toMatchObject({
      label: "Fix rejected", tone: "bad", title: "breaks 2 tests",
    });
    expect(outcomeOf(incident({ patch_status: "valid" }))).toMatchObject({ label: "Patch not verified", tone: "warn" });
    expect(outcomeOf(incident({ patch_status: "invalid" }))).toMatchObject({ label: "Patch did not apply", tone: "warn" });
    expect(outcomeOf(incident({ patch_status: "skipped" }))).toMatchObject({ label: "No patch", tone: "muted" });
    expect(outcomeOf(incident())).toMatchObject({ label: "No patch", tone: "muted" });
  });

  it("filters on every word, across the fields people search by", () => {
    const list = [
      incident({
        title: "Race on stock", rule_id: "RACE003", kind: "RACE003", file: "orders/service.py", owasp: null, root_cause: "No lock.",
        symbol: "orders.service.Inventory.reserve",
      }),
      incident({ title: "SQL injection", file: "orders/db.py" }),
      incident({ title: "Weak hash", rule_id: "B324", kind: "B324", owasp: "A02:2021", file: "auth/hash.py", root_cause: null, symbol: null }),
    ];
    expect(filterIncidents(list, "")).toBe(list);
    expect(filterIncidents(list, "  ")).toBe(list);
    expect(filterIncidents(list, "race").map((i) => i.title)).toEqual(["Race on stock"]);
    expect(filterIncidents(list, "A03").map((i) => i.title)).toEqual(["SQL injection"]);
    expect(filterIncidents(list, "orders db").map((i) => i.title)).toEqual(["SQL injection"]); // every word must match
    expect(filterIncidents(list, "NO LOCK").map((i) => i.title)).toEqual(["Race on stock"]);
    expect(filterIncidents(list, "A02 hash")).toHaveLength(1);
    expect(filterIncidents(list, "nonsense")).toEqual([]);
  });

  it("summarizes runs, patterns and outcomes", () => {
    const list = [
      incident({ signature: "a", run_id: "r1", regression_passed: true }),
      incident({ signature: "a", run_id: "r2", regression_passed: true }),
      incident({ signature: "b", run_id: "r2", regression_passed: false }),
      incident({ signature: "c", run_id: "r3" }),
    ];
    expect(historyStats(list)).toEqual({ incidents: 4, patterns: 3, runs: 3, verified: 2, rejected: 1, repeated: 1 });
    expect(historyStats([])).toEqual({ incidents: 0, patterns: 0, runs: 0, verified: 0, rejected: 0, repeated: 0 });
  });

  it("formats how long ago something happened", () => {
    const now = Date.parse("2026-10-04T12:00:00+00:00");
    const ago = (seconds: number) => relativeTime(new Date(now - seconds * 1000).toISOString(), now);
    expect([ago(5), ago(120), ago(3 * 3600), ago(2 * 86400)]).toEqual(["just now", "2 min ago", "3 h ago", "2 d ago"]);
    expect(relativeTime("2026-10-04T12:00:30+00:00", now)).toBe("just now"); // a clock a little ahead is not "in the future"
    expect(relativeTime("not a date", now)).toBe("not a date");
  });

  it("classifies diff lines", () => {
    const kinds = ["--- a/x.py", "+++ b/x.py", "@@ -1,2 +1,2 @@", "+added", "-removed", " context", ""].map(diffLineKind);
    expect(kinds).toEqual(["meta", "meta", "hunk", "add", "del", "ctx", "ctx"]);
  });
});
