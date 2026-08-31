/**
 * Polls GET /api/jobs/{id} while the job is alive: a setTimeout chain (never two requests
 * in flight), 2 s between polls growing by 1.5x up to 10 s, stopped as soon as the status
 * is terminal, aborted on unmount. A 404 means the job is unknown or expired ("missing");
 * a 503 means the signal branch is disabled on this server ("unavailable"). Both stop the
 * polling. Transient errors keep the last good status and keep polling.
 */
import { useCallback, useEffect, useState } from "react";
import { ApiError, describeError, getJob } from "../../api/client";
import { isTerminal, type JobStatus } from "../../api/types";
import { nextPollDelay, POLL_INITIAL_MS, POLL_MAX_MS } from "./signalModel";

export type JobPollState =
  | { status: "loading"; job: null; error: null }
  /** `error` is a transient poll failure since the last good status (still polling). */
  | { status: "ok"; job: JobStatus; error: string | null }
  | { status: "missing"; job: null; error: string }
  /** 503: the nanopore signal branch is not enabled on this server (polling stopped). */
  | { status: "unavailable"; job: null; error: string }
  /** No status yet and the request failed for another reason (still polling). */
  | { status: "error"; job: null; error: string };

export interface JobPolling {
  state: JobPollState;
  /** Restart polling immediately (e.g. after a cancel request). */
  refresh: () => void;
  /** Adopt a status returned by another call (POST cancel) without waiting for a poll. */
  replace: (job: JobStatus) => void;
}

export interface JobPollingOptions {
  initialMs?: number;
  maxMs?: number;
}

const LOADING: JobPollState = { status: "loading", job: null, error: null };

export function useJobPolling(jobId: string | null, opts: JobPollingOptions = {}): JobPolling {
  const initialMs = opts.initialMs ?? POLL_INITIAL_MS;
  const maxMs = opts.maxMs ?? POLL_MAX_MS;
  const [state, setState] = useState<JobPollState>(LOADING);
  const [generation, setGeneration] = useState(0);

  const refresh = useCallback(() => setGeneration((g) => g + 1), []);
  const replace = useCallback((job: JobStatus) => setState({ status: "ok", job, error: null }), []);

  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let delay = initialMs;

    const schedule = () => {
      timer = window.setTimeout(() => void tick(), delay);
      delay = Math.min(maxMs, nextPollDelay(delay));
    };

    const tick = async (): Promise<void> => {
      try {
        const job = await getJob(jobId, controller.signal);
        if (cancelled) return;
        setState({ status: "ok", job, error: null });
        if (!isTerminal(job.status)) schedule();
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 422)) {
          setState({ status: "missing", job: null, error: describeError(err) });
          return;
        }
        if (err instanceof ApiError && err.status === 503) {
          setState({ status: "unavailable", job: null, error: describeError(err) });
          return;
        }
        const message = describeError(err);
        setState((prev) =>
          prev.status === "ok" ? { ...prev, error: message } : { status: "error", job: null, error: message },
        );
        schedule();
      }
    };

    void tick();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [jobId, generation, initialMs, maxMs]);

  return { state, refresh, replace };
}
