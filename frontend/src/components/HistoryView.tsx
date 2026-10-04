import { ChevronRight, History, Loader2, RefreshCw, Repeat, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";

import { getIncidents } from "../api/client";
import type { Incident, Incidents } from "../api/types";
import { diffLineKind, filterIncidents, historyStats, outcomeOf, relativeTime } from "../history";
import { SEVERITY_COLORS } from "../stages";
import { IncidentSkeleton } from "./Skeletons";

interface HistoryViewProps {
  /** Reload when this changes (a run finished) or when the tab becomes visible. */
  refreshKey: number;
  active: boolean;
}

export function HistoryView({ refreshKey, active }: HistoryViewProps) {
  const [data, setData] = useState<Incidents | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await getIncidents());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) void load();
  }, [active, refreshKey, load]);

  const all = data?.incidents ?? [];
  const shown = useMemo(() => filterIncidents(all, query), [all, query]);
  const stats = useMemo(() => historyStats(all), [all]);

  return (
    <section className="history" aria-label="Incident history">
      <header className="history__header">
        <div className="history__title">
          <History size={16} /> Incident history
        </div>
        <div className="history__stats">
          <span>
            <strong>{stats.incidents}</strong> incidents
          </span>
          <span>
            <strong>{stats.patterns}</strong> patterns
          </span>
          <span>
            <strong>{stats.runs}</strong> runs
          </span>
          <span className="history__stat--good">
            <strong>{stats.verified}</strong> fixes verified
          </span>
          {stats.rejected > 0 && (
            <span className="history__stat--bad">
              <strong>{stats.rejected}</strong> rejected
            </span>
          )}
          {stats.repeated > 0 && (
            <span className="history__stat--seen">
              <strong>{stats.repeated}</strong> patterns seen again
            </span>
          )}
        </div>
        <label className="history__search">
          <Search size={14} aria-hidden />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by rule, file, OWASP, root cause…"
            spellCheck={false}
            aria-label="Search incidents"
          />
        </label>
        <button className="icon-btn" onClick={() => void load()} title="Reload" aria-label="Reload history">
          {loading ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
        </button>
      </header>

      <div className="history__body">
        {!error && !data && <IncidentSkeleton />}
        {error && <div className="history__empty history__empty--error">{error}</div>}
        {!error && data && !data.enabled && (
          <div className="history__empty">Incident memory is switched off (CODELOOP_MEMORY=off), so nothing is recorded or recalled.</div>
        )}
        {!error && data?.enabled && all.length === 0 && (
          <div className="history__empty">
            <History size={34} strokeWidth={1.4} />
            <div className="history__empty-title">No incidents yet</div>
            Every finding in application code is remembered after a run. When a pattern comes back, it is flagged on the graph and its past fixes
            inform the new patch.
          </div>
        )}
        {all.length > 0 && shown.length === 0 && <div className="history__empty">Nothing matches “{query}”.</div>}
        {shown.length > 0 && (
          <ul className="incidents">
            {shown.map((incident) => (
              <IncidentRow key={incident.id} incident={incident} open={open === incident.id} onToggle={() => setOpen(open === incident.id ? null : incident.id)} />
            ))}
          </ul>
        )}
        {data && data.total > all.length && <div className="history__more">Showing the newest {all.length} of {data.total} incidents.</div>}
      </div>
    </section>
  );
}

function IncidentRow({ incident: i, open, onToggle }: { incident: Incident; open: boolean; onToggle: () => void }) {
  const outcome = outcomeOf(i);
  return (
    <li className={open ? "incident is-open" : "incident"} style={{ "--sev": SEVERITY_COLORS[i.severity] } as CSSProperties}>
      <button className="incident__row" onClick={onToggle} aria-expanded={open}>
        <ChevronRight size={14} className="incident__chevron" aria-hidden />
        <span className="badge" style={{ "--badge-color": SEVERITY_COLORS[i.severity] } as CSSProperties}>
          {i.severity}
        </span>
        <span className="incident__what">
          <span className="incident__title">{i.title}</span>
          <span className="incident__where">
            {i.file}:{i.line}
            {i.symbol && <span className="incident__symbol"> · {i.symbol.split(".").slice(-2).join(".")}</span>}
          </span>
        </span>
        <span className="tag tag--mono">{i.kind}</span>
        {i.owasp ? <span className="tag tag--owasp">{i.owasp.split(":")[0]}</span> : <span />}
        {i.occurrences > 1 ? (
          <span className="seen" title={`${i.occurrences} incidents share this signature`}>
            <Repeat size={11} /> ×{i.occurrences}
          </span>
        ) : (
          <span />
        )}
        <span className={`outcome outcome--${outcome.tone}`} title={outcome.title}>
          {outcome.label}
        </span>
        <span className="incident__when" title={new Date(i.created_at).toLocaleString()}>
          {relativeTime(i.created_at)}
        </span>
      </button>

      {open && (
        <div className="incident__details">
          <dl>
            <dt>Signature</dt>
            <dd className="mono">
              {i.signature} <span className="faint">({i.kind} · {i.owasp ?? "no OWASP category"})</span>
            </dd>
            <dt>Code pattern</dt>
            <dd>
              <code className="pattern">{i.pattern}</code>
            </dd>
            <dt>Run</dt>
            <dd className="mono">
              {i.run_id} <span className="faint">· {i.source}</span>
            </dd>
          </dl>
          {i.root_cause && (
            <>
              <h4>Root cause</h4>
              <p>{i.root_cause}</p>
            </>
          )}
          {i.patch_design && (
            <>
              <h4>Hardened design</h4>
              <p>{i.patch_design}</p>
            </>
          )}
          {i.regression_reasons.length > 0 && (
            <>
              <h4>Verification notes</h4>
              <ul className="notes">
                {i.regression_reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </>
          )}
          {i.patch_diff && (
            <>
              <h4>
                Patch <span className={`outcome outcome--${outcome.tone}`}>{outcome.label}</span>
              </h4>
              <pre className="diff">
                {i.patch_diff.split("\n").map((line, n) => (
                  <div key={n} className={`diff__line diff__line--${diffLineKind(line)}`}>
                    {line || " "}
                  </div>
                ))}
              </pre>
            </>
          )}
        </div>
      )}
    </li>
  );
}
