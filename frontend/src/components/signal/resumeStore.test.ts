import { describe, expect, it } from "vitest";
import {
  clearJobResumeEntries,
  findResumableJob,
  loadResumeMap,
  removeResumeEntries,
  RESUME_STORAGE_KEY,
  saveResumeEntries,
} from "./resumeStore";

function memStorage(initial: Record<string, string> = {}) {
  const m = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
    dump: () => Object.fromEntries(m),
  };
}

const now = Date.parse("2026-08-31T12:00:00Z");
const files = {
  pod5: { name: "a.pod5", size: 10, lastModified: 1 },
  bam: { name: "a.bam", size: 20, lastModified: 2 },
  reference: { name: "r.fa", size: 30, lastModified: 3 },
  regions: { name: "reg.csv", size: 40, lastModified: 4 },
};
const entries = {
  "a.pod5|10|1": { jobId: "job-1", slot: "pod5" as const, uploadUrl: "/api/uploads/1", savedAt: new Date(now).toISOString() },
  "a.bam|20|2": { jobId: "job-1", slot: "bam" as const, uploadUrl: "/api/uploads/2", savedAt: new Date(now).toISOString() },
  "r.fa|30|3": { jobId: "job-1", slot: "reference" as const, uploadUrl: "/api/uploads/3", savedAt: new Date(now).toISOString() },
  "reg.csv|40|4": { jobId: "job-1", slot: "regions" as const, uploadUrl: "/api/uploads/4", savedAt: new Date(now).toISOString() },
};

describe("resumeStore", () => {
  it("round-trips entries and finds the job when all four files match", () => {
    const store = memStorage();
    saveResumeEntries(entries, store);
    const map = loadResumeMap(store, now);
    expect(Object.keys(map)).toHaveLength(4);
    const found = findResumableJob(files, map);
    expect(found?.jobId).toBe("job-1");
    expect(found?.entries.bam.uploadUrl).toBe("/api/uploads/2");
  });

  it("does not offer a resume when a file differs, is missing, or belongs to another job", () => {
    const map = loadResumeMap(memStorage({ [RESUME_STORAGE_KEY]: JSON.stringify(entries) }), now);
    expect(findResumableJob({ ...files, bam: { ...files.bam, size: 21 } }, map)).toBeNull();
    expect(findResumableJob({ ...files, regions: undefined }, map)).toBeNull();
    const mixed = { ...entries, "a.bam|20|2": { ...entries["a.bam|20|2"], jobId: "job-2" } };
    expect(findResumableJob(files, mixed)).toBeNull();
    // Slot mismatch (same file picked into another slot) is not a match either.
    const swapped = { ...entries, "a.bam|20|2": { ...entries["a.bam|20|2"], slot: "pod5" as const } };
    expect(findResumableJob(files, swapped)).toBeNull();
  });

  it("prunes entries older than 48 h and survives corrupt or disabled storage", () => {
    const old = { ...entries["a.pod5|10|1"], savedAt: new Date(now - 49 * 3600 * 1000).toISOString() };
    const map = loadResumeMap(memStorage({ [RESUME_STORAGE_KEY]: JSON.stringify({ ...entries, "a.pod5|10|1": old }) }), now);
    expect(map["a.pod5|10|1"]).toBeUndefined();
    expect(Object.keys(map)).toHaveLength(3);
    expect(loadResumeMap(memStorage({ [RESUME_STORAGE_KEY]: "{not json" }), now)).toEqual({});
    expect(loadResumeMap(memStorage({ [RESUME_STORAGE_KEY]: JSON.stringify({ x: { jobId: 1 } }) }), now)).toEqual({});
    expect(loadResumeMap(null, now)).toEqual({});
    const throwing = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("SecurityError");
      },
      removeItem: () => undefined,
    };
    expect(loadResumeMap(throwing, now)).toEqual({});
    expect(() => saveResumeEntries(entries, throwing)).not.toThrow();
  });

  it("honours a shorter server TTL (capabilities.limits.upload_ttl_h) when pruning", () => {
    const old = { ...entries["a.pod5|10|1"], savedAt: new Date(now - 13 * 3600 * 1000).toISOString() };
    const store = memStorage({ [RESUME_STORAGE_KEY]: JSON.stringify({ ...entries, "a.pod5|10|1": old }) });
    expect(Object.keys(loadResumeMap(store, now))).toHaveLength(4); // 13 h < 48 h default
    expect(Object.keys(loadResumeMap(store, now, 12 * 3600 * 1000))).toHaveLength(3);
  });

  it("removes entries by fingerprint and by job, deleting the key when empty", () => {
    const store = memStorage();
    saveResumeEntries(entries, store);
    removeResumeEntries(["a.pod5|10|1"], store);
    expect(Object.keys(loadResumeMap(store, now))).toHaveLength(3);
    clearJobResumeEntries("job-1", store);
    expect(store.dump()).toEqual({});
  });
});
