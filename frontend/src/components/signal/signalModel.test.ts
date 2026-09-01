import { describe, expect, it } from "vitest";
import signalResults from "../../api/fixtures/signal_results.json";
import readsAll from "../../api/fixtures/signal_reads_all.json";
import jobRunning from "../../api/fixtures/job_running.json";
import type { SignalRead, SignalResultsMeta, SignalSite } from "../../api/types";
import {
  elapsedSeconds,
  extraNumber,
  formatDuration,
  isCalled,
  isUuid,
  lowCoverageCount,
  nextPollDelay,
  READ_CSV_HEADER,
  readsToCsv,
  STAGE_INFO,
  STAGE_ORDER,
  totalStageSeconds,
  transcriptMeta,
  transcriptsOf,
} from "./signalModel";

const sites = signalResults.results as SignalSite[];
const meta = signalResults.meta as unknown as SignalResultsMeta;

describe("signalModel", () => {
  it("poll delay grows by 1.5x and caps at 10 s", () => {
    const seq = [2000];
    for (let i = 0; i < 6; i++) seq.push(nextPollDelay(seq[seq.length - 1]));
    expect(seq).toEqual([2000, 3000, 4500, 6750, 10000, 10000, 10000]);
  });

  it("every stage has a label and a one-line explanation", () => {
    for (const s of STAGE_ORDER) {
      expect(STAGE_INFO[s].label.length).toBeGreaterThan(0);
      expect(STAGE_INFO[s].explanation).toMatch(/\.$/);
    }
    expect(STAGE_ORDER).toEqual(["uploading", "preparing", "sampling", "features", "denovo", "inference", "aggregating"]);
  });

  it("formats durations and elapsed time", () => {
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(59.4)).toBe("59 s");
    expect(formatDuration(185)).toBe("3 min 05 s");
    expect(formatDuration(3720)).toBe("1 h 02 min");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(-1)).toBe("—");
    const t0 = Date.parse(jobRunning.started_at as string);
    expect(elapsedSeconds(jobRunning as never, t0 + 95_000)).toBe(95);
    expect(elapsedSeconds({ created_at: "2026-01-01T00:00:00Z", started_at: null, finished_at: "2026-01-01T00:01:00Z" }, 0)).toBe(60);
  });

  it("validates job ids as UUIDs", () => {
    expect(isUuid(jobRunning.job_id)).toBe(true);
    expect(isUuid("00000000-0000-4000-8000-000000000000")).toBe(true);
    expect(isUuid("not-a-job")).toBe(false);
    expect(isUuid("")).toBe(false);
    expect(isUuid("../etc/passwd")).toBe(false);
  });

  it("counts sites below the coverage threshold (fixture: 4 of 22 below 30)", () => {
    expect(sites).toHaveLength(22);
    expect(lowCoverageCount(sites, meta.low_coverage_threshold)).toBe(4);
    expect(lowCoverageCount(sites, 10)).toBe(0);
    expect(lowCoverageCount([{ ...sites[0], coverage: null }], 30)).toBe(0);
  });

  it("builds a per-transcript PredictionMeta covering the whole transcript", () => {
    const tx = transcriptsOf(meta, sites);
    expect(tx.map((t) => t.transcript_id)).toEqual(["tx_A", "tx_B"]);
    const m = transcriptMeta(meta, tx[0], 14);
    expect(m).toMatchObject({
      sequence_length: 1200,
      predicted_start: 1,
      predicted_end: 1200,
      alpha: 1,
      n_sites: 14,
      source: "signal",
      transcript_id: "tx_A",
      attention: null,
    });
    expect(m.mod_types).toEqual(["ac4C", "m1A", "m5C", "m6A", "m7G", "Psi"]);
    // Derived from rows when meta.transcripts is empty.
    const derived = transcriptsOf({ transcripts: [] }, sites);
    expect(derived.map((t) => [t.transcript_id, t.n_sites])).toEqual([
      ["tx_A", 14],
      ["tx_B", 8],
    ]);
    expect(derived[0].length).toBe(1180);
  });

  it("read-level CSV matches the server header and marks calls above 0.5", () => {
    const reads = readsAll as SignalRead[];
    const csv = readsToCsv(reads.slice(0, 2));
    const lines = csv.trimEnd().split("\n");
    expect(lines[0]).toBe(READ_CSV_HEADER.join(","));
    expect(lines[0]).toBe("read_id,transcript_id,position,strand,mod_type,probability,source");
    expect(lines[1]).toBe(`${reads[0].read_id},tx_A,101,+,m6A,${reads[0].probability},signal`);
    expect(reads.filter(isCalled)).toHaveLength(31);
  });

  it("reads numbers and stage seconds out of meta.extra", () => {
    expect(extraNumber(meta.extra, "regions_skipped_low_coverage")).toBe(1);
    expect(extraNumber({ x: ["a", "b"] }, "x")).toBe(2);
    expect(extraNumber({ x: "7" }, "x")).toBe(7);
    expect(extraNumber({}, "x")).toBeNull();
    expect(totalStageSeconds(meta.extra)).toBeCloseTo(164.3, 5);
    expect(totalStageSeconds({ stage_seconds: JSON.stringify({ a: 1, b: 2 }) })).toBe(3);
    expect(totalStageSeconds({})).toBeNull();
  });
});
