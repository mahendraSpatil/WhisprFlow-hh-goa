import type { Incident } from "./api/types";

export type OutcomeTone = "good" | "bad" | "warn" | "muted";

export interface Outcome {
  label: string;
  tone: OutcomeTone;
  title: string;
}

/** What became of an incident: was there a patch, and did it hold up under the regression check. */
export function outcomeOf(i: Incident): Outcome {
  if (i.regression_passed === true) {
    return { label: "Fix verified", tone: "good", title: "A patch was generated and passed the regression check" };
  }
  if (i.regression_passed === false) {
    return { label: "Fix rejected", tone: "bad", title: i.regression_reasons[0] ?? "The patch failed the regression check" };
  }
  if (i.patch_status === "valid") {
    return { label: "Patch not verified", tone: "warn", title: "A patch applied cleanly but was never run through the regression check" };
  }
  if (i.patch_status === "invalid") {
    return { label: "Patch did not apply", tone: "warn", title: "Claude's diff did not apply, even after a retry" };
  }
  return { label: "No patch", tone: "muted", title: "No patch was produced for this incident" };
}

/** Case-insensitive match of every word in the query against the fields a person would search by. */
export function filterIncidents(incidents: Incident[], query: string): Incident[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return incidents;
  return incidents.filter((i) => {
    const haystack = [i.title, i.kind, i.rule_id, i.owasp, i.file, i.symbol, i.root_cause, i.signature, i.severity, i.category, i.source]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return words.every((w) => haystack.includes(w));
  });
}

export interface HistoryStats {
  incidents: number;
  patterns: number;
  runs: number;
  verified: number;
  rejected: number;
  repeated: number;
}

export function historyStats(incidents: Incident[]): HistoryStats {
  const bySignature = new Map<string, number>();
  for (const i of incidents) bySignature.set(i.signature, (bySignature.get(i.signature) ?? 0) + 1);
  return {
    incidents: incidents.length,
    patterns: bySignature.size,
    runs: new Set(incidents.map((i) => i.run_id)).size,
    verified: incidents.filter((i) => i.regression_passed === true).length,
    rejected: incidents.filter((i) => i.regression_passed === false).length,
    repeated: [...bySignature.values()].filter((n) => n > 1).length,
  };
}

export function relativeTime(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - Date.parse(iso)) / 1000);
  if (Number.isNaN(seconds)) return iso;
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return new Date(iso).toLocaleDateString();
}

/** One line of a unified diff: which way to color it. */
export function diffLineKind(line: string): "add" | "del" | "hunk" | "meta" | "ctx" {
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}
