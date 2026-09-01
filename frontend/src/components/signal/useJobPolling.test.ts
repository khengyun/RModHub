import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import jobDone from "../../api/fixtures/job_done.json";
import jobRunning from "../../api/fixtures/job_running.json";
import type { JobStatus } from "../../api/types";
import { useJobPolling } from "./useJobPolling";

const JOB = jobRunning.job_id;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Queue of responses; the last one repeats. */
function fetchSequence(responses: (() => Response | Promise<Response>)[]) {
  let i = 0;
  const mock = vi.fn(async (_input: RequestInfo | URL) => {
    const make = responses[Math.min(i, responses.length - 1)];
    i += 1;
    return make();
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

const flush = () => act(() => vi.advanceTimersByTimeAsync(0));
const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useJobPolling", () => {
  it("polls with a growing interval (2 s -> 3 s -> 4.5 s ... <= 10 s) and stops on a terminal status", async () => {
    const fetchMock = fetchSequence([
      () => jsonResponse(jobRunning),
      () => jsonResponse(jobRunning),
      () => jsonResponse(jobRunning),
      () => jsonResponse(jobDone),
    ]);
    const { result } = renderHook(() => useJobPolling(JOB));
    expect(result.current.state.status).toBe("loading");

    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe(`/api/jobs/${JOB}`);
    expect(result.current.state.status).toBe("ok");
    expect(result.current.state.job?.status).toBe("running");

    await advance(1999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(2); // t = 2 s

    await advance(2999);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(3); // + 3 s

    await advance(4500);
    expect(fetchMock).toHaveBeenCalledTimes(4); // + 4.5 s -> done
    expect(result.current.state.job?.status).toBe("done");

    await advance(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(4); // terminal: no more polling
  });

  it("caps the interval at 10 s", async () => {
    const fetchMock = fetchSequence([() => jsonResponse(jobRunning)]);
    renderHook(() => useJobPolling(JOB));
    await flush();
    // Delays: 2, 3, 4.5, 6.75, 10, 10, 10 ... (cumulative 2, 5, 9.5, 16.25, 26.25, 36.25)
    await advance(16_250);
    expect(fetchMock).toHaveBeenCalledTimes(5);
    await advance(9_999);
    expect(fetchMock).toHaveBeenCalledTimes(5);
    await advance(1);
    expect(fetchMock).toHaveBeenCalledTimes(6);
    await advance(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(7);
  });

  it("maps a 404 to 'missing' and stops polling", async () => {
    const fetchMock = fetchSequence([() => jsonResponse({ detail: "Unknown or expired job." }, 404)]);
    const { result } = renderHook(() => useJobPolling(JOB));
    await flush();
    expect(result.current.state.status).toBe("missing");
    expect(result.current.state.error).toBe("Unknown or expired job.");
    await advance(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("maps a 503 (signal branch disabled) to 'unavailable' and stops polling", async () => {
    const fetchMock = fetchSequence([
      () => jsonResponse({ detail: "The nanopore signal branch is not enabled on this server." }, 503),
    ]);
    const { result } = renderHook(() => useJobPolling(JOB));
    await flush();
    expect(result.current.state.status).toBe("unavailable");
    expect(result.current.state.error).toMatch(/not enabled on this server/);
    await advance(60_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the last good status through a transient failure and keeps polling", async () => {
    const fetchMock = fetchSequence([
      () => jsonResponse(jobRunning),
      () => {
        throw new TypeError("Failed to fetch");
      },
      () => jsonResponse(jobDone),
    ]);
    const { result } = renderHook(() => useJobPolling(JOB));
    await flush();
    await advance(2000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.state.status).toBe("ok");
    expect(result.current.state.job?.status).toBe("running");
    expect(result.current.state.error).toMatch(/Cannot reach/);
    await advance(3000);
    expect(result.current.state.job?.status).toBe("done");
    expect(result.current.state.error).toBeNull();
  });

  it("aborts the in-flight request and the timer on unmount; a null id never polls", async () => {
    const fetchMock = fetchSequence([() => jsonResponse(jobRunning)]);
    const { unmount } = renderHook(() => useJobPolling(JOB));
    await flush();
    unmount();
    await advance(30_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    renderHook(() => useJobPolling(null));
    await advance(30_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("replace() adopts a status returned elsewhere and refresh() polls again immediately", async () => {
    const fetchMock = fetchSequence([() => jsonResponse(jobRunning)]);
    const { result } = renderHook(() => useJobPolling(JOB));
    await flush();
    act(() => result.current.replace({ ...(jobDone as unknown as JobStatus), status: "cancelled" }));
    expect(result.current.state.job?.status).toBe("cancelled");
    act(() => result.current.refresh());
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
