import { describe, expect, it, vi } from "vitest";
import { DEFAULT_RETRY_DELAYS_MS, fileFingerprint, tusDelete, tusHead, tusUpload, TusError } from "./tus";

/* ----------------------------------------------------------------------------------------
 * A minimal in-memory tus server behind a fake XMLHttpRequest.
 * -------------------------------------------------------------------------------------- */

interface FakeRequest {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: Blob | null;
}
type FakeResponse = { status: number; headers?: Record<string, string> } | { error: true } | { hang: true };
type Handler = (req: FakeRequest) => FakeResponse | Promise<FakeResponse>;

function fakeXhrFactory(handler: Handler, log: FakeRequest[] = []) {
  return () => {
    const state = {
      method: "",
      url: "",
      headers: {} as Record<string, string>,
      resHeaders: {} as Record<string, string>,
      aborted: false,
    };
    const xhr = {
      status: 0,
      upload: { onprogress: null as ((e: ProgressEvent) => void) | null },
      onload: null as (() => void) | null,
      onerror: null as (() => void) | null,
      onabort: null as (() => void) | null,
      ontimeout: null as (() => void) | null,
      open(method: string, url: string) {
        state.method = method;
        state.url = url;
      },
      setRequestHeader(k: string, v: string) {
        state.headers[k] = v;
      },
      getResponseHeader(name: string) {
        const k = Object.keys(state.resHeaders).find((h) => h.toLowerCase() === name.toLowerCase());
        return k ? state.resHeaders[k] : null;
      },
      abort() {
        state.aborted = true;
        xhr.onabort?.();
      },
      send(body: Blob | null) {
        const req: FakeRequest = { method: state.method, url: state.url, headers: { ...state.headers }, body };
        log.push(req);
        void Promise.resolve(handler(req)).then((res) => {
          if (state.aborted) return;
          if ("hang" in res) return;
          if ("error" in res) {
            xhr.onerror?.();
            return;
          }
          if (body && xhr.upload.onprogress) xhr.upload.onprogress({ loaded: body.size } as ProgressEvent);
          xhr.status = res.status;
          state.resHeaders = res.headers ?? {};
          xhr.onload?.();
        });
      },
    };
    return xhr as unknown as XMLHttpRequest;
  };
}

/** tus core semantics: HEAD reports the offset, PATCH must match it. */
function tusServer(initialOffset = 0) {
  const server = { offset: initialOffset, received: [] as number[] };
  const handler: Handler = async (req): Promise<FakeResponse> => {
    if (req.method === "HEAD") return { status: 200, headers: { "Upload-Offset": String(server.offset), "Upload-Length": "40" } };
    if (req.method === "PATCH") {
      expect(req.headers["Tus-Resumable"]).toBe("1.0.0");
      expect(req.headers["Content-Type"]).toBe("application/offset+octet-stream");
      if (Number(req.headers["Upload-Offset"]) !== server.offset) return { status: 409 };
      server.offset += req.body?.size ?? 0;
      server.received.push(server.offset);
      return { status: 204, headers: { "Upload-Offset": String(server.offset) } };
    }
    return { status: 405 };
  };
  return { server, handler };
}

const instant = async () => undefined;
const file = () => new Blob([new Uint8Array(40)]);

describe("tusUpload", () => {
  it("sends 16 MiB-style chunks sequentially with the right headers and reports progress", async () => {
    const { server, handler } = tusServer();
    const log: FakeRequest[] = [];
    const progress: number[] = [];
    await tusUpload({
      url: "/api/uploads/u1",
      file: file(),
      offset: 0,
      chunkSize: 16,
      xhrFactory: fakeXhrFactory(handler, log),
      onProgress: (sent) => progress.push(sent),
      sleep: instant,
    });
    const patches = log.filter((r) => r.method === "PATCH");
    expect(patches.map((r) => r.headers["Upload-Offset"])).toEqual(["0", "16", "32"]);
    expect(patches.map((r) => r.body?.size)).toEqual([16, 16, 8]);
    expect(log.filter((r) => r.method === "HEAD")).toHaveLength(0); // offset was known
    expect(server.offset).toBe(40);
    expect(progress.at(-1)).toBe(40);
    expect(progress.every((p, i, a) => i === 0 || p >= a[i - 1])).toBe(true);
  });

  it("starts with a HEAD when no offset is known and skips the bytes already there", async () => {
    const { server, handler } = tusServer(24);
    const log: FakeRequest[] = [];
    await tusUpload({ url: "/u", file: file(), chunkSize: 16, xhrFactory: fakeXhrFactory(handler, log), sleep: instant });
    expect(log[0].method).toBe("HEAD");
    expect(log.filter((r) => r.method === "PATCH").map((r) => r.headers["Upload-Offset"])).toEqual(["24"]);
    expect(server.offset).toBe(40);
  });

  it("recovers from a 409 offset mismatch by asking the server (HEAD) and continuing from there", async () => {
    const { server, handler } = tusServer(10); // server already has 10 bytes; client thinks 0
    const log: FakeRequest[] = [];
    const retries: number[] = [];
    await tusUpload({
      url: "/u",
      file: file(),
      offset: 0,
      chunkSize: 16,
      xhrFactory: fakeXhrFactory(handler, log),
      onRetry: ({ delayMs }) => retries.push(delayMs),
      sleep: instant,
    });
    const patchOffsets = log.filter((r) => r.method === "PATCH").map((r) => r.headers["Upload-Offset"]);
    expect(patchOffsets).toEqual(["0", "10", "26"]);
    expect(log.filter((r) => r.method === "HEAD")).toHaveLength(1);
    expect(retries).toEqual([0]);
    expect(server.offset).toBe(40);
  });

  it("retries a network error with the 0/1/3/5/10/20/30/60 s schedule, resynchronising with HEAD each time", async () => {
    const { server, handler } = tusServer();
    let failures = 2;
    const flaky: Handler = (req) => {
      if (req.method === "PATCH" && failures > 0) {
        failures -= 1;
        return { error: true };
      }
      return handler(req);
    };
    const log: FakeRequest[] = [];
    const delays: number[] = [];
    const sleep = vi.fn(async (ms: number) => {
      delays.push(ms);
    });
    await tusUpload({ url: "/u", file: file(), offset: 0, chunkSize: 40, xhrFactory: fakeXhrFactory(flaky, log), sleep });
    expect(delays).toEqual([0, 1000]);
    expect(log.map((r) => r.method)).toEqual(["PATCH", "HEAD", "PATCH", "HEAD", "PATCH"]);
    expect(server.offset).toBe(40);
  });

  it("aborts a PATCH that accepts no byte for stallMs and retries it via HEAD (stalled connection)", async () => {
    const { server, handler } = tusServer();
    let hangs = 1;
    const stalling: Handler = (req) => {
      if (req.method === "PATCH" && hangs > 0) {
        hangs -= 1;
        return { hang: true }; // the request neither progresses nor answers
      }
      return handler(req);
    };
    const log: FakeRequest[] = [];
    const retries: { delayMs: number; message: string; offline: boolean }[] = [];
    await tusUpload({
      url: "/u",
      file: file(),
      offset: 0,
      chunkSize: 40,
      stallMs: 20,
      xhrFactory: fakeXhrFactory(stalling, log),
      onRetry: ({ delayMs, error, offline }) => retries.push({ delayMs, message: error.message, offline }),
      sleep: instant,
    });
    expect(log.map((r) => r.method)).toEqual(["PATCH", "HEAD", "PATCH"]);
    expect(retries).toHaveLength(1);
    expect(retries[0].message).toMatch(/stalled/);
    expect(retries[0].offline).toBe(false);
    expect(server.offset).toBe(40);
  });

  it("waits for the browser to come back online instead of spending retries while offline", async () => {
    const { server, handler } = tusServer();
    let online = false;
    let failures = 1;
    const flaky: Handler = (req) => {
      if (req.method === "PATCH" && failures > 0) {
        failures -= 1;
        return { error: true };
      }
      return handler(req);
    };
    const log: FakeRequest[] = [];
    const delays: number[] = [];
    const retries: boolean[] = [];
    let waited = 0;
    await tusUpload({
      url: "/u",
      file: file(),
      offset: 0,
      chunkSize: 40,
      retryDelaysMs: [], // no retry budget at all: only the offline wait can save the upload
      xhrFactory: fakeXhrFactory(flaky, log),
      isOnline: () => online,
      waitForOnline: async () => {
        waited += 1;
        online = true;
      },
      onRetry: ({ offline }) => retries.push(offline),
      sleep: async (ms) => {
        delays.push(ms);
      },
    });
    expect(waited).toBe(1);
    expect(delays).toEqual([]);
    expect(retries).toEqual([true]);
    expect(log.map((r) => r.method)).toEqual(["PATCH", "HEAD", "PATCH"]);
    expect(server.offset).toBe(40);
  });

  it("gives up after the last retry delay and surfaces a TusError", async () => {
    const dead: Handler = (req) => (req.method === "HEAD" ? { status: 200, headers: { "Upload-Offset": "0" } } : { error: true });
    const delays: number[] = [];
    const err = await tusUpload({
      url: "/u",
      file: file(),
      offset: 0,
      xhrFactory: fakeXhrFactory(dead),
      sleep: async (ms) => {
        delays.push(ms);
      },
    }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(TusError);
    expect((err as TusError).kind).toBe("network");
    expect(delays).toEqual([...DEFAULT_RETRY_DELAYS_MS]);
  });

  it("does not retry a 404/410 (upload gone) or another fatal 4xx", async () => {
    const gone: Handler = () => ({ status: 404 });
    const log: FakeRequest[] = [];
    const err = await tusUpload({ url: "/u", file: file(), offset: 0, xhrFactory: fakeXhrFactory(gone, log), sleep: instant }).catch(
      (e: unknown) => e,
    );
    expect((err as TusError).kind).toBe("gone");
    expect(log).toHaveLength(1);

    const tooBig: Handler = () => ({ status: 413 });
    const err2 = await tusUpload({ url: "/u", file: file(), offset: 0, xhrFactory: fakeXhrFactory(tooBig), sleep: instant }).catch(
      (e: unknown) => e,
    );
    expect((err2 as TusError).status).toBe(413);
  });

  it("aborting through the AbortSignal cancels the in-flight PATCH and rejects with kind 'aborted'", async () => {
    const controller = new AbortController();
    const hang: Handler = (req) => (req.method === "PATCH" ? { hang: true } : { status: 200, headers: { "Upload-Offset": "0" } });
    const log: FakeRequest[] = [];
    const pending = tusUpload({ url: "/u", file: file(), offset: 0, xhrFactory: fakeXhrFactory(hang, log), signal: controller.signal, sleep: instant });
    await new Promise((r) => setTimeout(r, 0));
    expect(log).toHaveLength(1);
    controller.abort();
    const err = await pending.catch((e: unknown) => e);
    expect((err as TusError).kind).toBe("aborted");
    // An already-aborted signal never sends anything.
    const err2 = await tusUpload({ url: "/u", file: file(), offset: 0, xhrFactory: fakeXhrFactory(hang, log), signal: controller.signal }).catch(
      (e: unknown) => e,
    );
    expect((err2 as TusError).kind).toBe("aborted");
    expect(log).toHaveLength(1);
  });

  it("a file the server already has completely resolves after a single HEAD", async () => {
    const { handler } = tusServer(40);
    const log: FakeRequest[] = [];
    await tusUpload({ url: "/u", file: file(), xhrFactory: fakeXhrFactory(handler, log), sleep: instant });
    expect(log.map((r) => r.method)).toEqual(["HEAD"]);
  });
});

describe("tusHead", () => {
  it("parses Upload-Offset / Upload-Length and maps 404 to 'gone'", async () => {
    const ok: Handler = () => ({ status: 200, headers: { "upload-offset": "12", "Upload-Length": "40" } });
    expect(await tusHead("/u", { xhrFactory: fakeXhrFactory(ok) })).toEqual({ offset: 12, length: 40 });
    const gone: Handler = () => ({ status: 404 });
    const err = await tusHead("/u", { xhrFactory: fakeXhrFactory(gone) }).catch((e: unknown) => e);
    expect((err as TusError).kind).toBe("gone");
  });
});

describe("tusDelete", () => {
  it("sends DELETE with Tus-Resumable and treats 204 and an already-gone upload (404) as success", async () => {
    const log: FakeRequest[] = [];
    await tusDelete("/api/uploads/u1", { xhrFactory: fakeXhrFactory(() => ({ status: 204 }), log) });
    expect(log).toHaveLength(1);
    expect(log[0].method).toBe("DELETE");
    expect(log[0].url).toBe("/api/uploads/u1");
    expect(log[0].headers["Tus-Resumable"]).toBe("1.0.0");
    await expect(tusDelete("/u", { xhrFactory: fakeXhrFactory(() => ({ status: 404 })) })).resolves.toBeUndefined();
    const err = await tusDelete("/u", { xhrFactory: fakeXhrFactory(() => ({ status: 409 })) }).catch((e: unknown) => e);
    expect((err as TusError).kind).toBe("http");
    expect((err as TusError).status).toBe(409);
  });
});

describe("fileFingerprint", () => {
  it("is stable for the same name/size/mtime and differs otherwise", () => {
    const a = { name: "a.pod5", size: 10, lastModified: 1 };
    expect(fileFingerprint(a)).toBe(fileFingerprint({ ...a }));
    expect(fileFingerprint(a)).not.toBe(fileFingerprint({ ...a, size: 11 }));
    expect(fileFingerprint(a)).not.toBe(fileFingerprint({ ...a, lastModified: 2 }));
  });
});
