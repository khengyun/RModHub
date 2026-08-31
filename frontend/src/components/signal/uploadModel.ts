/**
 * Pure logic behind the nanopore signal upload form: slot definitions, client-side
 * validation against the server's caps, per-slot progress state and the size estimate
 * for the pod5 subset helper. No React, no DOM: everything is unit-tested.
 */
import { UPLOAD_SLOTS, type CapabilityLimits, type UploadSlot } from "../../api/types";

export const KB = 1024;
export const MB = 1024 * 1024;
export const GB = 1024 * 1024 * 1024;

export interface FileLike {
  name: string;
  size: number;
  lastModified?: number;
}

export interface SlotDef {
  id: UploadSlot;
  label: string;
  /** Short name for progress lines. */
  short: string;
  /** `accept` attribute of the file input. */
  accept: string;
  extensions: readonly string[];
  hint: string;
}

export const SLOT_DEFS: readonly SlotDef[] = [
  {
    id: "pod5",
    label: "Raw signal (pod5)",
    short: "pod5",
    accept: ".pod5",
    extensions: [".pod5"],
    hint: "The pod5 file(s) merged into one file; only reads overlapping your regions are needed.",
  },
  {
    id: "bam",
    label: "Basecalled, aligned BAM with move table",
    short: "BAM",
    accept: ".bam",
    extensions: [".bam"],
    hint: "dorado basecaller … --emit-moves --reference ref.fa, then samtools sort. The index is built on the server.",
  },
  {
    id: "reference",
    label: "Reference FASTA",
    short: "reference",
    accept: ".fa,.fasta",
    extensions: [".fa", ".fasta"],
    hint: "The transcript sequences the BAM was aligned to (.fa or .fasta, uncompressed).",
  },
  {
    id: "regions",
    label: "Regions CSV",
    short: "regions",
    accept: ".csv,text/csv",
    extensions: [".csv"],
    hint: "Header seqnames,start,end,width,strand; 1-based inclusive coordinates; one region per line.",
  },
];

export function slotDef(id: UploadSlot): SlotDef {
  const def = SLOT_DEFS.find((d) => d.id === id);
  if (!def) throw new Error(`unknown upload slot ${id}`);
  return def;
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n >= GB) return `${(n / GB).toFixed(n >= 10 * GB ? 1 : 2)} GB`;
  if (n >= MB) return `${(n / MB).toFixed(n >= 100 * MB ? 0 : 1)} MB`;
  if (n >= KB) return `${(n / KB).toFixed(0)} KB`;
  return `${n} B`;
}

export function extensionOk(name: string, extensions: readonly string[]): boolean {
  const lower = name.toLowerCase();
  return extensions.some((ext) => lower.endsWith(ext));
}

/** Effective per-slot byte cap from the server's capabilities (regions has no byte cap). */
export function slotCapBytes(slot: UploadSlot, limits: CapabilityLimits): number | null {
  switch (slot) {
    case "pod5":
      return limits.max_pod5_gb * GB;
    case "bam":
      return (limits.max_bam_gb ?? limits.max_pod5_gb) * GB;
    case "reference":
      return limits.max_reference_mb * MB;
    case "regions":
      return null;
  }
}

export function capLabel(slot: UploadSlot, limits: CapabilityLimits): string {
  switch (slot) {
    case "pod5":
      return `${limits.max_pod5_gb} GB`;
    case "bam":
      return `${limits.max_bam_gb ?? limits.max_pod5_gb} GB`;
    case "reference":
      return `${limits.max_reference_mb} MB`;
    case "regions":
      return `${limits.max_regions.toLocaleString("en-US")} rows`;
  }
}

export interface SelectionValidation {
  /** One message per slot that has a problem (missing, wrong type, too large, empty). */
  slotErrors: Partial<Record<UploadSlot, string>>;
  /** One-line summary for the form, or null when there is nothing to say yet. */
  summary: string | null;
  /** All four files chosen (valid or not). */
  complete: boolean;
  /** Ready to call /init. */
  ok: boolean;
}

const MISSING: Record<UploadSlot, string> = {
  pod5: "Select the pod5 signal file.",
  bam: "A BAM file is required: DirectRM needs the basecalled, aligned reads (dorado --emit-moves) that belong to this pod5.",
  reference: "Select the reference FASTA the BAM was aligned to.",
  regions: "Select the regions CSV (seqnames,start,end,width,strand).",
};

export function validateSelection(
  files: Partial<Record<UploadSlot, FileLike>>,
  limits: CapabilityLimits,
): SelectionValidation {
  const slotErrors: Partial<Record<UploadSlot, string>> = {};
  const picked = UPLOAD_SLOTS.filter((s) => files[s] !== undefined);
  const complete = picked.length === UPLOAD_SLOTS.length;

  for (const slot of UPLOAD_SLOTS) {
    const f = files[slot];
    const def = slotDef(slot);
    if (!f) {
      // Only nag about a missing BAM/reference/regions once something was picked.
      if (picked.length > 0) slotErrors[slot] = MISSING[slot];
      continue;
    }
    if (!extensionOk(f.name, def.extensions)) {
      slotErrors[slot] = `Expected a ${def.extensions.join(" or ")} file, got "${f.name}".`;
      continue;
    }
    if (f.size <= 0) {
      slotErrors[slot] = `"${f.name}" is empty.`;
      continue;
    }
    const cap = slotCapBytes(slot, limits);
    if (cap !== null && f.size > cap) {
      slotErrors[slot] =
        slot === "reference"
          ? `${formatBytes(f.size)} exceeds the ${capLabel(slot, limits)} limit of this server; keep only the transcripts named in your regions.`
          : `${formatBytes(f.size)} exceeds the ${capLabel(slot, limits)} limit of this server. Use the subset tool below to keep only the reads in your regions.`;
    }
  }

  const nErrors = Object.keys(slotErrors).length;
  let summary: string | null = null;
  if (picked.length > 0 && !complete) {
    summary =
      files.pod5 && !files.bam
        ? "A pod5 file alone cannot be analysed: add the matching BAM, the reference FASTA and the regions CSV."
        : "All four files are required.";
  } else if (nErrors > 0) {
    summary = "Fix the highlighted files before uploading.";
  }
  return { slotErrors, summary, complete, ok: complete && nErrors === 0 };
}

/* ----------------------------------------------------------------------------------------
 * "My pod5 is too big"
 * -------------------------------------------------------------------------------------- */

/** Copy-paste command of the subset tool (tools/README; image built locally). */
export const SUBSET_DOCKER_COMMAND =
  'docker run --rm -v "$PWD:/data" rmodhub/subset:local -i /data/big.pod5 -b /data/in.bam -r /data/reg.csv -o /data/small.pod5 --bam-out /data/small.bam';

/**
 * Rough size of the subset pod5: the per-read signal dominates the file, so the size
 * scales with the fraction of reads kept. Returns MB (1 decimal) or null for bad input.
 */
export function estimateSubsetMb(pod5Bytes: number, totalReads: number, readsInRegions: number): number | null {
  if (![pod5Bytes, totalReads, readsInRegions].every((v) => Number.isFinite(v) && v >= 0)) return null;
  if (pod5Bytes === 0 || totalReads === 0) return null;
  const frac = Math.min(1, readsInRegions / totalReads);
  return Math.round((pod5Bytes * frac) / MB * 10) / 10;
}

/* ----------------------------------------------------------------------------------------
 * Per-slot progress
 * -------------------------------------------------------------------------------------- */

export type SlotStatus = "waiting" | "uploading" | "paused" | "done" | "error";

export interface SlotProgress {
  status: SlotStatus;
  sent: number;
  total: number;
  /** Error text (status "error") or a transient note ("retrying in 3 s"). */
  message: string | null;
}

export function initialSlotProgress(total = 0): SlotProgress {
  return { status: "waiting", sent: 0, total, message: null };
}

export function percent(p: Pick<SlotProgress, "sent" | "total">): number {
  if (p.total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.floor((p.sent / p.total) * 100)));
}

export function slotStatusText(p: SlotProgress): string {
  switch (p.status) {
    case "waiting":
      return "waiting";
    case "uploading":
      return p.message ? `uploading ${percent(p)}% (${p.message})` : `uploading ${percent(p)}%`;
    case "paused":
      return `paused at ${percent(p)}%`;
    case "done":
      return "done";
    case "error":
      return `error: ${p.message ?? "upload failed"}`;
  }
}

export function overallPercent(slots: Record<UploadSlot, SlotProgress>): number {
  let sent = 0;
  let total = 0;
  for (const s of UPLOAD_SLOTS) {
    sent += Math.min(slots[s].sent, slots[s].total);
    total += slots[s].total;
  }
  return percent({ sent, total });
}

export function allDone(slots: Record<UploadSlot, SlotProgress>): boolean {
  return UPLOAD_SLOTS.every((s) => slots[s].status === "done");
}

/**
 * Text of the page's single live region for the upload. Progress events fire several
 * times a second, so the text only carries the overall percentage rounded down to 10 %
 * steps and each slot's status (not its percentage): a screen reader hears transitions
 * (done, paused, error) and coarse progress, not hundreds of near-identical updates.
 */
export function uploadAnnouncement(slots: Record<UploadSlot, SlotProgress>): string {
  const overall = Math.floor(overallPercent(slots) / 10) * 10;
  const parts = UPLOAD_SLOTS.map((s) => `${slotDef(s).short} ${slots[s].status}`);
  return `Upload ${overall}% overall: ${parts.join(", ")}.`;
}
