import type { HealthState } from "./useHealth";

type Status = HealthState["status"];

const LABEL: Record<Status, string> = {
  checking: "checking server…",
  loading: "model loading…",
  ready: "model ready",
  unreachable: "server unreachable",
};

const DOT: Record<Status, string> = {
  checking: "bg-slate-400",
  loading: "bg-amber-500 animate-pulse",
  ready: "bg-emerald-600",
  unreachable: "bg-red-600",
};

/** Small coloured dot + label in the header; never blocks anything. */
export function HealthIndicator({ state }: { state: HealthState }) {
  const title =
    state.status === "ready"
      ? `${state.health.model_name} ${state.health.model_version} · API ${state.health.version}`
      : state.status === "unreachable"
        ? "GET /health failed. Predictions will not work until the server is reachable again."
        : "GET /health is polled every 10 s until the model is loaded.";
  return (
    <span
      data-testid="health-indicator"
      data-status={state.status}
      title={title}
      className="inline-flex items-center gap-1.5 text-xs text-slate-600"
    >
      <span aria-hidden className={`inline-block h-2 w-2 rounded-full ${DOT[state.status]}`} />
      <span role="status" aria-live="polite">
        {LABEL[state.status]}
      </span>
    </span>
  );
}
