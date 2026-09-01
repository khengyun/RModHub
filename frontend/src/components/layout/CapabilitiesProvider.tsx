/**
 * GET /api/capabilities, fetched at app load and cached in context. Tells the shell
 * whether the nanopore signal branch is enabled and carries the limits / retention facts
 * that the upload page and the result page render verbatim.
 *
 * Three outcomes:
 * - the server answered: `status: "ready"` with its capabilities;
 * - the route does not exist (404, an API without the signal branch): `status: "ready"`
 *   with `signal: false`, so the sequence branch keeps working exactly as before;
 * - anything else (network error, 5xx from a restarting API, non-JSON body): `status:
 *   "unavailable"`. This is *not* "the branch is disabled": the app retries with a growing
 *   delay (2 s -> 30 s) and exposes `refetch()` for a manual retry, and the pages say that
 *   the server could not be reached.
 */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError, describeError, getCapabilities } from "../../api/client";
import type { Capabilities } from "../../api/types";

/** Contract defaults (docs/signal-branch.md section 8), used until the server answers. */
export const DEFAULT_CAPABILITIES: Capabilities = {
  sequence: true,
  signal: false,
  limits: {
    max_pod5_gb: 5,
    max_reference_mb: 500,
    max_regions: 10_000,
    max_running_per_ip: 1,
    max_queued_per_ip: 3,
    job_timeout_h: 6,
    tus_chunk_mb: 64,
    upload_ttl_h: 48,
  },
  retention: { inputs_deleted: "after feature extraction, at most 48 h", results_days: 14 },
};

/** Hours an unfinished upload is kept on the server (`limits.upload_ttl_h`, default 48). */
export function uploadTtlHours(capabilities: Capabilities): number {
  const h = capabilities.limits.upload_ttl_h;
  return typeof h === "number" && Number.isFinite(h) && h > 0 ? h : 48;
}

export type CapabilitiesStatus = "loading" | "ready" | "unavailable";

export interface CapabilitiesState {
  status: CapabilitiesStatus;
  capabilities: Capabilities;
  /** Why the server could not be asked (status "unavailable"). */
  error?: string | null;
}

export interface CapabilitiesContextValue extends CapabilitiesState {
  /** Ask the server again now (status goes back to "loading"). */
  refetch: () => void;
}

export const CAPABILITIES_RETRY_INITIAL_MS = 2_000;
export const CAPABILITIES_RETRY_MAX_MS = 30_000;

const LOADING: CapabilitiesState = { status: "loading", capabilities: DEFAULT_CAPABILITIES, error: null };
const UNAVAILABLE: CapabilitiesState = { status: "unavailable", capabilities: DEFAULT_CAPABILITIES, error: null };

const CapabilitiesContext = createContext<CapabilitiesContextValue>({ ...UNAVAILABLE, refetch: () => undefined });

/** Fill in any limit the server left out so the UI never prints "undefined GB". */
export function normalizeCapabilities(raw: Partial<Capabilities> | null | undefined): Capabilities {
  return {
    sequence: raw?.sequence ?? true,
    signal: raw?.signal === true,
    limits: { ...DEFAULT_CAPABILITIES.limits, ...(raw?.limits ?? {}) },
    retention: { ...DEFAULT_CAPABILITIES.retention, ...(raw?.retention ?? {}) },
  };
}

export function CapabilitiesProvider({
  children,
  value,
}: {
  children: ReactNode;
  /** Test hook: skip the fetch and provide a fixed state. */
  value?: CapabilitiesState;
}) {
  const [state, setState] = useState<CapabilitiesState>(value ?? LOADING);
  const [generation, setGeneration] = useState(0);

  const refetch = useCallback(() => {
    setState(LOADING);
    setGeneration((g) => g + 1);
  }, []);

  useEffect(() => {
    if (value) return;
    const controller = new AbortController();
    let cancelled = false;
    let timer: number | undefined;
    let delay = CAPABILITIES_RETRY_INITIAL_MS;

    const attempt = async (): Promise<void> => {
      try {
        const caps = await getCapabilities(controller.signal);
        if (cancelled) return;
        setState({ status: "ready", capabilities: normalizeCapabilities(caps), error: null });
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) {
          // An older API without the route: the signal branch is absent, not unreachable.
          setState({ status: "ready", capabilities: DEFAULT_CAPABILITIES, error: null });
          return;
        }
        setState({ ...UNAVAILABLE, error: describeError(err) });
        timer = window.setTimeout(() => void attempt(), delay);
        delay = Math.min(CAPABILITIES_RETRY_MAX_MS, delay * 2);
      }
    };
    void attempt();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [value, generation]);

  const current = value ?? state;
  return (
    <CapabilitiesContext.Provider value={{ ...current, error: current.error ?? null, refetch }}>
      {children}
    </CapabilitiesContext.Provider>
  );
}

export function useCapabilities(): CapabilitiesContextValue {
  return useContext(CapabilitiesContext);
}
