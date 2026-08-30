import { afterEach, describe, expect, it, vi } from "vitest";
import errChars from "./fixtures/err_chars.json";
import errShort from "./fixtures/err_short.json";
import { ApiError, describeError, predictSequence } from "./client";

function mockFetch(status: number, body: unknown, headers: Record<string, string> = {}) {
  const res = new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(res));
}

afterEach(() => vi.unstubAllGlobals());

describe("API error mapping", () => {
  it("surfaces the backend's plain-language 422 detail", async () => {
    mockFetch(errShort.status, errShort.body);
    await expect(predictSequence({ sequence: "ACGT" })).rejects.toMatchObject({
      status: 422,
      detail: expect.stringContaining("at least 51"),
    });
    mockFetch(errChars.status, errChars.body);
    const err = await predictSequence({ sequence: "ACGTN" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(describeError(err)).toContain("'N'");
  });

  it("replaces 503 with a model-loading message", async () => {
    mockFetch(503, { detail: "model not loaded" });
    const err = await predictSequence({ sequence: "ACGT" }).catch((e: unknown) => e);
    expect(describeError(err)).toMatch(/still loading/);
  });

  it("explains network failures without leaking internals", () => {
    expect(describeError(new TypeError("Failed to fetch"))).toMatch(/Cannot reach/);
    expect(describeError(new DOMException("aborted", "AbortError"))).toBe("Request cancelled.");
  });
});
