import { describe, expect, it } from "vitest";
import capabilities from "../../api/fixtures/capabilities.json";
import type { CapabilityLimits, UploadSlot } from "../../api/types";
import {
  allDone,
  capLabel,
  estimateSubsetMb,
  extensionOk,
  formatBytes,
  GB,
  initialSlotProgress,
  MB,
  overallPercent,
  percent,
  SLOT_DEFS,
  slotCapBytes,
  slotStatusText,
  SUBSET_DOCKER_COMMAND,
  uploadAnnouncement,
  validateSelection,
  type SlotProgress,
} from "./uploadModel";

const limits = capabilities.limits as CapabilityLimits;
const f = (name: string, size = 1000) => ({ name, size, lastModified: 1 });
const full = () => ({
  pod5: f("run.pod5", 5_000_000),
  bam: f("run.bam", 100_000),
  reference: f("ref.fa", 4_000),
  regions: f("regions.csv", 90),
});

describe("validateSelection", () => {
  it("is silent with nothing picked and complete/ok with four good files", () => {
    const none = validateSelection({}, limits);
    expect(none).toEqual({ slotErrors: {}, summary: null, complete: false, ok: false });
    const ok = validateSelection(full(), limits);
    expect(ok.ok).toBe(true);
    expect(ok.complete).toBe(true);
    expect(ok.summary).toBeNull();
  });

  it("pod5 without a BAM: the BAM slot says a BAM is required and the summary explains", () => {
    const v = validateSelection({ pod5: f("run.pod5") }, limits);
    expect(v.ok).toBe(false);
    expect(v.slotErrors.bam).toMatch(/A BAM file is required/);
    expect(v.slotErrors.bam).toMatch(/emit-moves/);
    expect(v.slotErrors.reference).toMatch(/reference FASTA/);
    expect(v.slotErrors.regions).toMatch(/regions CSV/);
    expect(v.summary).toMatch(/pod5 file alone cannot be analysed/);
  });

  it("checks extensions per slot (case-insensitive, .fa and .fasta both fine)", () => {
    const v = validateSelection({ ...full(), pod5: f("run.fast5"), reference: f("ref.fna") }, limits);
    expect(v.slotErrors.pod5).toBe('Expected a .pod5 file, got "run.fast5".');
    expect(v.slotErrors.reference).toBe('Expected a .fa or .fasta file, got "ref.fna".');
    expect(v.slotErrors.bam).toBeUndefined();
    expect(v.summary).toBe("Fix the highlighted files before uploading.");
    expect(validateSelection({ ...full(), reference: f("REF.FASTA"), bam: f("X.BAM") }, limits).ok).toBe(true);
  });

  it("applies the server caps with a clear message and points at the subset tool", () => {
    const v = validateSelection({ ...full(), pod5: f("big.pod5", 6 * GB) }, limits);
    expect(v.slotErrors.pod5).toMatch(/^6\.00 GB exceeds the 5 GB limit/);
    expect(v.slotErrors.pod5).toMatch(/subset tool/);
    const r = validateSelection({ ...full(), reference: f("ref.fa", 501 * MB) }, limits);
    expect(r.slotErrors.reference).toMatch(/exceeds the 500 MB limit/);
    // BAM falls back to the pod5 cap when max_bam_gb is absent, and honours it when present.
    expect(validateSelection({ ...full(), bam: f("a.bam", 5.5 * GB) }, limits).slotErrors.bam).toMatch(/5 GB limit/);
    expect(validateSelection({ ...full(), bam: f("a.bam", 5.5 * GB) }, { ...limits, max_bam_gb: 8 }).ok).toBe(true);
    expect(capLabel("regions", limits)).toBe("10,000 rows");
    expect(slotCapBytes("regions", limits)).toBeNull();
  });

  it("rejects empty files", () => {
    const v = validateSelection({ ...full(), regions: f("regions.csv", 0) }, limits);
    expect(v.slotErrors.regions).toBe('"regions.csv" is empty.');
  });

  it("all four files partially picked -> 'All four files are required.'", () => {
    const v = validateSelection({ bam: f("a.bam"), reference: f("r.fa") }, limits);
    expect(v.summary).toBe("All four files are required.");
    expect(v.slotErrors.pod5).toMatch(/pod5/);
  });
});

describe("size estimate and formatting", () => {
  it("estimateSubsetMb scales the pod5 size by the fraction of reads kept", () => {
    expect(estimateSubsetMb(10 * GB, 1_000_000, 10_000)).toBe(102.4);
    expect(estimateSubsetMb(2 * GB, 500, 1000)).toBe(2048); // capped at 100 %
    expect(estimateSubsetMb(0, 10, 5)).toBeNull();
    expect(estimateSubsetMb(10 * GB, 0, 5)).toBeNull();
    expect(estimateSubsetMb(Number.NaN, 1, 1)).toBeNull();
    expect(estimateSubsetMb(10 * GB, 100, -1)).toBeNull();
  });

  it("formatBytes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("2 KB");
    expect(formatBytes(1.5 * MB)).toBe("1.5 MB");
    expect(formatBytes(250 * MB)).toBe("250 MB");
    expect(formatBytes(1.25 * GB)).toBe("1.25 GB");
    expect(formatBytes(12.3 * GB)).toBe("12.3 GB");
    expect(formatBytes(-1)).toBe("—");
  });

  it("the docker command matches the contract shape", () => {
    expect(SUBSET_DOCKER_COMMAND).toBe(
      'docker run --rm -v "$PWD:/data" rmodhub/subset:local -i /data/big.pod5 -b /data/in.bam -r /data/reg.csv -o /data/small.pod5 --bam-out /data/small.bam',
    );
  });

  it("slot definitions cover the four slots with accept attributes", () => {
    expect(SLOT_DEFS.map((d) => d.id)).toEqual(["pod5", "bam", "reference", "regions"]);
    expect(extensionOk("a.FA", SLOT_DEFS[2].extensions)).toBe(true);
    expect(extensionOk("a.fa.gz", SLOT_DEFS[2].extensions)).toBe(false);
  });
});

describe("slot progress", () => {
  const slots = (): Record<UploadSlot, SlotProgress> => ({
    pod5: { status: "uploading", sent: 50, total: 100, message: null },
    bam: { status: "done", sent: 100, total: 100, message: null },
    reference: { status: "waiting", sent: 0, total: 100, message: null },
    regions: { status: "error", sent: 20, total: 100, message: "Network error during the upload." },
  });

  it("status text", () => {
    const s = slots();
    expect(slotStatusText(s.pod5)).toBe("uploading 50%");
    expect(slotStatusText({ ...s.pod5, message: "retry 1 in 3 s" })).toBe("uploading 50% (retry 1 in 3 s)");
    expect(slotStatusText(s.bam)).toBe("done");
    expect(slotStatusText(s.reference)).toBe("waiting");
    expect(slotStatusText(s.regions)).toBe("error: Network error during the upload.");
    expect(slotStatusText({ ...s.pod5, status: "paused" })).toBe("paused at 50%");
    expect(percent(initialSlotProgress(0))).toBe(0);
  });

  it("overall percent and allDone", () => {
    expect(overallPercent(slots())).toBe(42);
    expect(allDone(slots())).toBe(false);
    const done = slots();
    for (const k of Object.keys(done) as UploadSlot[]) done[k] = { status: "done", sent: 100, total: 100, message: null };
    expect(allDone(done)).toBe(true);
    expect(overallPercent(done)).toBe(100);
  });
});

describe("uploadAnnouncement", () => {
  it("changes only on slot status changes and 10 % steps, never per byte", () => {
    const slots = (pod5Sent: number): Record<UploadSlot, SlotProgress> => ({
      pod5: { status: "uploading", sent: pod5Sent, total: 1000, message: null },
      bam: { status: "done", sent: 0, total: 0, message: null },
      reference: { status: "waiting", sent: 0, total: 0, message: null },
      regions: { status: "paused", sent: 0, total: 0, message: null },
    });
    expect(uploadAnnouncement(slots(0))).toBe("Upload 0% overall: pod5 uploading, BAM done, reference waiting, regions paused.");
    expect(uploadAnnouncement(slots(1))).toBe(uploadAnnouncement(slots(99)));
    expect(uploadAnnouncement(slots(100))).toMatch(/^Upload 10% overall/);
    expect(uploadAnnouncement(slots(199))).toMatch(/^Upload 10% overall/);
  });
});
