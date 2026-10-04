import {
  ArrowRight,
  Brain,
  Check,
  FlaskConical,
  GitBranch,
  Layers,
  Microscope,
  Network,
  Play,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { Fragment, useEffect, useState } from "react";

import { getEnvironment } from "../api/client";
import type { Environment, StageName } from "../api/types";
import { capabilities } from "../capabilities";
import { PHASES, STAGES, STAGE_DETAILS } from "../stages";

const STAGE_ICONS: Record<StageName, LucideIcon> = {
  repo_scout: GitBranch,
  system_analyst: Layers,
  pipeline_architect: Network,
  sandbox_runner: FlaskConical,
  diagnostic_sentinel: ShieldAlert,
  root_cause_diagnostician: Microscope,
  patch_master: Wrench,
  regression_check: ShieldCheck,
  memory_keeper: Brain,
};

const STAGE_INFO = Object.fromEntries(STAGES.map((s, i) => [s.name, { ...s, index: i + 1 }])) as Record<StageName, (typeof STAGES)[number] & { index: number }>;

type EnvState = { kind: "loading" } | { kind: "ready"; env: Environment } | { kind: "unreachable"; message: string };

function useEnvironment(): EnvState {
  const [state, setState] = useState<EnvState>({ kind: "loading" });
  useEffect(() => {
    let alive = true;
    getEnvironment().then(
      (env) => alive && setState({ kind: "ready", env }),
      (err) => alive && setState({ kind: "unreachable", message: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      alive = false;
    };
  }, []);
  return state;
}

/** What the canvas shows before the first run: what CodeLoop does, the nine stages, and a one-click demo. */
export function Landing({ onRun }: { onRun: (source: string) => void }) {
  const environment = useEnvironment();
  const demo = environment.kind === "ready" ? environment.env.demo_path : null;

  return (
    <div className="canvas canvas--landing">
      <div className="landing">
        <header className="landing__hero">
          <div className="landing__kicker">Autonomous code lifecycle</div>
          <h1 className="landing__title">From a failing repo to a verified pull request</h1>
          <p className="landing__lead">
            CodeLoop reads a Python repository, runs its tests in isolation, finds what breaks, traces each failure to a line, writes a minimal
            patch, proves the patch on a fresh copy, and remembers the incident for next time.
          </p>
          <div className="landing__actions">
            {demo && (
              <button type="button" className="btn btn--primary" onClick={() => onRun(demo)}>
                <Play size={15} fill="currentColor" /> Analyze the demo app
              </button>
            )}
            <span className="landing__or">
              {demo ? "or paste a git URL or a local path above" : "Paste a git URL or a local path above and press Run"}
              <ArrowRight size={13} aria-hidden />
            </span>
          </div>
          {demo && (
            <p className="landing__demo-note">
              The demo is a small order-processing app with three planted bugs: a division by zero, a SQL injection and a race condition.
            </p>
          )}
        </header>

        <section className="landing__pipeline" aria-label="The nine stages">
          {PHASES.map((phase, p) => (
            <Fragment key={phase.key}>
              <div className="phase">
                <div className="phase__head">
                  <span className="phase__num">{p + 1}</span>
                  <span className="phase__title">{phase.title}</span>
                  <span className="phase__blurb">{phase.blurb}</span>
                </div>
                <ol className="phase__stages">
                  {phase.stages.map((name) => {
                    const info = STAGE_INFO[name];
                    const Icon = STAGE_ICONS[name];
                    return (
                      <li key={name} className="lstage">
                        <span className="lstage__icon">
                          <Icon size={15} />
                        </span>
                        <div>
                          <div className="lstage__name">
                            <span className="lstage__index">{String(info.index).padStart(2, "0")}</span> {info.title}
                          </div>
                          <div className="lstage__detail">{STAGE_DETAILS[name]}</div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              </div>
              {p < PHASES.length - 1 && <ArrowRight className="phase__arrow" size={18} aria-hidden />}
            </Fragment>
          ))}
        </section>

        <section className="landing__env" aria-label="What is switched on" aria-busy={environment.kind === "loading"}>
          {environment.kind === "loading" &&
            [0, 1, 2].map((i) => <span key={i} className="skeleton skeleton--chip" aria-hidden />)}
          {environment.kind === "ready" &&
            capabilities(environment.env).map((c) => (
              <span key={c.key} className={c.on ? "cap cap--on" : "cap"} title={c.hint}>
                {c.on ? <Check size={12} /> : <X size={12} />} {c.label}
              </span>
            ))}
          {environment.kind === "unreachable" && (
            <span className="cap cap--error" role="alert">
              <TriangleAlert size={12} /> {environment.message}
            </span>
          )}
          {environment.kind === "ready" &&
            capabilities(environment.env)
              .filter((c) => !c.on)
              .map((c) => (
                <p key={c.key} className="landing__hint">
                  {c.hint}
                </p>
              ))}
        </section>
      </div>
    </div>
  );
}
