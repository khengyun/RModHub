/**
 * Best-effort resume of an interrupted upload across page reloads. A small map
 * fingerprint(name, size, lastModified) -> { jobId, slot, uploadUrl } lives in
 * localStorage (first-party storage, no cookie, nothing leaves the browser). Every
 * access is guarded: private mode / disabled storage simply disables the feature.
 */
import { fileFingerprint, type FileIdentity } from "../../api/tus";
import { UPLOAD_SLOTS, type UploadSlot } from "../../api/types";

export const RESUME_STORAGE_KEY = "rmodhub.signal.resume.v1";
/**
 * Default age after which a record is dropped: unfinished uploads expire on the server
 * after RMODHUB_UPLOAD_TTL_H (48 h by default). Callers pass the server's actual value
 * (`capabilities.limits.upload_ttl_h`) when they have it.
 */
export const RESUME_MAX_AGE_MS = 48 * 3600 * 1000;

export interface ResumeEntry {
  jobId: string;
  slot: UploadSlot;
  uploadUrl: string;
  /** ISO timestamp of when the entry was written. */
  savedAt: string;
}

export type ResumeMap = Record<string, ResumeEntry>;

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function defaultStorage(): StorageLike | null {
  try {
    return typeof window !== "undefined" && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
}

function isEntry(v: unknown): v is ResumeEntry {
  if (!v || typeof v !== "object") return false;
  const e = v as Record<string, unknown>;
  return (
    typeof e.jobId === "string" &&
    typeof e.uploadUrl === "string" &&
    typeof e.savedAt === "string" &&
    (UPLOAD_SLOTS as readonly string[]).includes(e.slot as string)
  );
}

export function loadResumeMap(
  store: StorageLike | null = defaultStorage(),
  now = Date.now(),
  maxAgeMs = RESUME_MAX_AGE_MS,
): ResumeMap {
  if (!store) return {};
  try {
    const raw = store.getItem(RESUME_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    const out: ResumeMap = {};
    for (const [fp, entry] of Object.entries(parsed as Record<string, unknown>)) {
      if (!isEntry(entry)) continue;
      const age = now - Date.parse(entry.savedAt);
      if (Number.isFinite(age) && age > maxAgeMs) continue;
      out[fp] = entry;
    }
    return out;
  } catch {
    return {};
  }
}

function write(map: ResumeMap, store: StorageLike | null): void {
  if (!store) return;
  try {
    if (Object.keys(map).length === 0) store.removeItem(RESUME_STORAGE_KEY);
    else store.setItem(RESUME_STORAGE_KEY, JSON.stringify(map));
  } catch {
    /* quota exceeded / disabled storage: resume is best effort */
  }
}

export function saveResumeEntries(entries: ResumeMap, store: StorageLike | null = defaultStorage()): void {
  write({ ...loadResumeMap(store), ...entries }, store);
}

export function removeResumeEntries(fingerprints: string[], store: StorageLike | null = defaultStorage()): void {
  const map = loadResumeMap(store);
  for (const fp of fingerprints) delete map[fp];
  write(map, store);
}

export function clearJobResumeEntries(jobId: string, store: StorageLike | null = defaultStorage()): void {
  const map = loadResumeMap(store);
  for (const [fp, e] of Object.entries(map)) if (e.jobId === jobId) delete map[fp];
  write(map, store);
}

export interface ResumableJob {
  jobId: string;
  entries: Record<UploadSlot, ResumeEntry>;
}

/**
 * When all four picked files were part of one earlier job (same fingerprints, same job
 * id, each in its own slot), that job can be resumed.
 */
export function findResumableJob(
  files: Partial<Record<UploadSlot, FileIdentity>>,
  map: ResumeMap,
): ResumableJob | null {
  const entries: Partial<Record<UploadSlot, ResumeEntry>> = {};
  let jobId: string | null = null;
  for (const slot of UPLOAD_SLOTS) {
    const f = files[slot];
    if (!f) return null;
    const e = map[fileFingerprint(f)];
    if (!e || e.slot !== slot) return null;
    if (jobId === null) jobId = e.jobId;
    else if (e.jobId !== jobId) return null;
    entries[slot] = e;
  }
  return jobId ? { jobId, entries: entries as Record<UploadSlot, ResumeEntry> } : null;
}
