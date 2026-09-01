/**
 * The two "no signal branch here" notices shared by /signal and /result/:jobId.
 *
 * - `SignalDisabled`: the server answered and the branch is off (capabilities.signal false,
 *   or a job route answered 503).
 * - `SignalUnavailable`: GET /api/capabilities could not be answered at all (network error,
 *   API restarting behind the proxy). That says nothing about the branch, so the notice
 *   offers a Retry and the provider keeps retrying by itself.
 */
import { Link } from "react-router-dom";
import { SIGNAL_DISABLED_MESSAGE } from "../../api/client";

export function SignalDisabled({ detail }: { detail?: string | null }) {
  return (
    <div data-testid="signal-disabled" className="max-w-2xl space-y-3 rounded border border-slate-200 bg-white px-4 py-4 text-sm text-slate-700">
      <h1 className="text-lg font-semibold text-brand-800">Nanopore signal</h1>
      <p role="status">{detail || SIGNAL_DISABLED_MESSAGE}</p>
      <p>
        The{" "}
        <Link to="/" className="text-brand-600 underline underline-offset-2">
          sequence branch
        </Link>{" "}
        (MultiRM) is available. Operators enable the signal branch by configuring the job database,
        the queue and a worker, see the deployment notes in the repository.
      </p>
    </div>
  );
}

export function SignalUnavailable({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <div
      data-testid="signal-unavailable"
      role="alert"
      className="max-w-2xl space-y-3 rounded border border-amber-300 bg-amber-50 px-4 py-4 text-sm text-amber-950"
    >
      <h1 className="text-lg font-semibold text-brand-800">Nanopore signal</h1>
      <p>
        <strong>Could not reach the server to check which analyses are available.</strong>
        {error ? ` ${error}` : ""} The page retries by itself; you can also try now.
      </p>
      <p className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-testid="signal-retry"
          onClick={onRetry}
          className="rounded bg-brand-600 px-3 py-1 text-xs font-medium text-white hover:bg-brand-700"
        >
          Retry
        </button>
        <Link to="/" className="text-brand-600 underline underline-offset-2">
          Use the sequence branch meanwhile
        </Link>
      </p>
    </div>
  );
}
