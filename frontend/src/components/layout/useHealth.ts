/**
 * Polls GET /health every `pollMs` until the model is loaded, then stops. Purely
 * informational: the header shows "model loading…" / "model ready" and the footer the
 * API version; nothing in the UI waits for it.
 */
import { useEffect, useState } from "react";
import { ApiError, getHealth } from "../../api/client";
import type { HealthResponse } from "../../api/types";

export type HealthState =
  | { status: "checking"; health: null }
  /** The API answers 503: process is up, model not in memory yet. */
  | { status: "loading"; health: null }
  | { status: "ready"; health: HealthResponse }
  /** Network error / proxy down; keeps polling. */
  | { status: "unreachable"; health: null };

export const HEALTH_POLL_MS = 10_000;

export function useHealth(pollMs: number = HEALTH_POLL_MS): HealthState {
  const [state, setState] = useState<HealthState>({ status: "checking", health: null });

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let cancelled = false;

    const tick = async (): Promise<void> => {
      let next: HealthState;
      try {
        const health = await getHealth(controller.signal);
        next = health.model_loaded
          ? { status: "ready", health }
          : { status: "loading", health: null };
      } catch (err) {
        if (cancelled) return;
        next =
          err instanceof ApiError && err.status === 503
            ? { status: "loading", health: null }
            : { status: "unreachable", health: null };
      }
      if (cancelled) return;
      setState(next);
      if (next.status !== "ready") timer = window.setTimeout(() => void tick(), pollMs);
    };

    void tick();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [pollMs]);

  return state;
}
