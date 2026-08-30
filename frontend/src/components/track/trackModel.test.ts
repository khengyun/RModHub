import { describe, expect, it } from "vitest";
import golden from "../../api/fixtures/golden_attention.json";
import many from "../../api/fixtures/many_rows.json";
import type { ModSite, PredictResponse } from "../../api/types";
import {
  BIN_PX,
  MIN_SPAN_NT,
  baseMismatch,
  binSites,
  centerView,
  clampView,
  fullView,
  glyphHeightFrac,
  glyphWidth,
  groupByType,
  isFullView,
  laneTypes,
  letterAt,
  lowerBound,
  makeScale,
  niceStep,
  overviewBins,
  panView,
  parseKeyPosition,
  shouldBin,
  spanView,
  ticksFor,
  visibleSlice,
  zoomView,
} from "./trackModel";

const goldenRes = golden as unknown as PredictResponse;
const manyRes = (many as unknown as { response: PredictResponse }).response;

function site(position: number, mod_type: string, probability = 0.5): ModSite {
  return { transcript_id: null, position, mod_type, probability, p_value: 0.01, coverage: null, source: "sequence" };
}

describe("viewport", () => {
  it("fullView covers [0.5, N + 0.5]", () => {
    expect(fullView(151)).toEqual({ start: 0.5, end: 151.5 });
    expect(isFullView(fullView(151), 151)).toBe(true);
  });

  it("clampView keeps the view inside the sequence and respects the minimum span", () => {
    expect(clampView({ start: -10, end: 40 }, 151)).toEqual({ start: 0.5, end: 50.5 });
    expect(clampView({ start: 140, end: 190 }, 151)).toEqual({ start: 101.5, end: 151.5 });
    const tiny = clampView({ start: 50, end: 51 }, 151);
    expect(tiny.end - tiny.start).toBe(MIN_SPAN_NT);
    const huge = clampView({ start: 0, end: 1e6 }, 151);
    expect(huge).toEqual({ start: 0.5, end: 151.5 });
  });

  it("zoomView keeps the anchor fixed and cannot zoom out past the full sequence", () => {
    const v = fullView(1000);
    const z = zoomView(v, 2, 300, 1000);
    expect(z.end - z.start).toBeCloseTo(500);
    // anchor at relative position (300-0.5)/1000 stays at the same relative position
    expect((300 - z.start) / (z.end - z.start)).toBeCloseTo((300 - 0.5) / 1000);
    expect(zoomView(z, 0.1, 300, 1000)).toEqual(fullView(1000));
  });

  it("panView / centerView / spanView stay in bounds", () => {
    const v = { start: 100.5, end: 200.5 };
    expect(panView(v, 50, 1000)).toEqual({ start: 150.5, end: 250.5 });
    expect(panView(v, -500, 1000)).toEqual({ start: 0.5, end: 100.5 });
    expect(centerView(v, 500, 1000)).toEqual({ start: 450, end: 550 });
    expect(spanView(10, 100, 1000)).toEqual({ start: 0.5, end: 100.5 });
    expect(spanView(995, 100, 1000)).toEqual({ start: 900.5, end: 1000.5 });
  });
});

describe("scale", () => {
  it("maps nucleotides to pixels and back", () => {
    const s = makeScale(fullView(100), 60, 1060); // 10 px per nt
    expect(s.pxPerNt).toBeCloseTo(10);
    expect(s.toPx(1)).toBeCloseTo(65); // centre of the first nucleotide
    expect(s.toPx(100)).toBeCloseTo(1055);
    expect(s.toNt(s.toPx(42))).toBeCloseTo(42);
  });
});

describe("ticks", () => {
  it("picks nice steps from the pixel density", () => {
    expect(niceStep(6.9)).toBe(10); // 151 nt over ~1040 px (default minPx = 55)
    expect(niceStep(0.104)).toBe(1000); // 10,000 nt over ~1040 px
    expect(niceStep(3)).toBe(25);
    expect(niceStep(60)).toBe(1);
    expect(niceStep(60, 70)).toBe(2);
  });

  it("151 nt at fit: labels 1, 10 … 140, 151 (150 dropped as it collides with 151)", () => {
    const n = 151;
    const s = makeScale(fullView(n), 64, 1100);
    const labels = ticksFor(fullView(n), n, s.pxPerNt).filter((t) => t.major).map((t) => t.pos);
    expect(labels[0]).toBe(1);
    expect(labels[labels.length - 1]).toBe(151);
    expect(labels).toContain(10);
    expect(labels).toContain(140);
    expect(labels).not.toContain(150);
    expect(labels).not.toContain(0);
  });

  it("10,000 nt at fit: labels every 1000 plus the ends", () => {
    const n = 10_000;
    const s = makeScale(fullView(n), 64, 1100);
    const ticks = ticksFor(fullView(n), n, s.pxPerNt);
    const labels = ticks.filter((t) => t.major).map((t) => t.label);
    expect(labels).toEqual(["1", "1,000", "2,000", "3,000", "4,000", "5,000", "6,000", "7,000", "8,000", "9,000", "10,000"]);
    // minor ticks every 200
    expect(ticks.some((t) => !t.major && t.pos === 200)).toBe(true);
    expect(ticks.every((t) => t.pos >= 1 && t.pos <= n)).toBe(true);
  });

  it("zoomed views only produce ticks inside the view and drop end labels that are not visible", () => {
    const n = 10_000;
    const v = { start: 4000.5, end: 4100.5 };
    const s = makeScale(v, 64, 1100);
    const ticks = ticksFor(v, n, s.pxPerNt);
    expect(ticks.length).toBeGreaterThan(3);
    expect(ticks.every((t) => t.pos >= 4001 && t.pos <= 4100)).toBe(true);
    expect(ticks.some((t) => t.label === "1")).toBe(false);
  });
});

describe("lanes", () => {
  it("orders lanes by MOD_TYPES and appends unknown types", () => {
    const sites = [site(5, "Psi"), site(3, "Am"), site(9, "m6A"), site(1, "zzz"), site(2, "Psi"), site(4, "newmod")];
    expect(laneTypes(sites)).toEqual(["Am", "m6A", "Psi", "newmod", "zzz"]);
    expect(laneTypes([])).toEqual([]);
  });

  it("golden fixture has 7 lanes; stacked positions land in different lanes", () => {
    expect(laneTypes(goldenRes.results)).toEqual(["Cm", "Gm", "Um", "m1A", "m5C", "m5U", "Psi"]);
    const groups = groupByType(goldenRes.results);
    const at123 = goldenRes.results.filter((s) => s.position === 123).map((s) => s.mod_type);
    expect(at123.sort()).toEqual(["Psi", "Um", "m5U"]);
    for (const t of at123) expect(groups.get(t)?.some((s) => s.position === 123)).toBe(true);
  });

  it("groupByType sorts each lane by position", () => {
    const groups = groupByType(manyRes.results);
    for (const arr of groups.values()) {
      for (let i = 1; i < arr.length; i++) expect(arr[i].position).toBeGreaterThanOrEqual(arr[i - 1].position);
    }
  });
});

describe("culling", () => {
  const sorted = [1, 5, 10, 10, 20, 50, 99].map((p) => site(p, "m6A"));

  it("lowerBound finds the first position >= lo", () => {
    expect(lowerBound(sorted, 0)).toBe(0);
    expect(lowerBound(sorted, 10)).toBe(2);
    expect(lowerBound(sorted, 11)).toBe(4);
    expect(lowerBound(sorted, 100)).toBe(7);
  });

  it("visibleSlice returns exactly the sites within [lo, hi]", () => {
    expect(visibleSlice(sorted, 5, 20).map((s) => s.position)).toEqual([5, 10, 10, 20]);
    expect(visibleSlice(sorted, 21, 49)).toEqual([]);
    expect(visibleSlice(sorted, -100, 1000)).toHaveLength(7);
    expect(visibleSlice([], 0, 10)).toEqual([]);
  });

  it("culls the 894-site fixture to a 50-nt window", () => {
    const groups = groupByType(manyRes.results);
    let total = 0;
    for (const arr of groups.values()) total += visibleSlice(arr, 100, 149).length;
    const expected = manyRes.results.filter((s) => s.position >= 100 && s.position <= 149).length;
    expect(total).toBe(expected);
    expect(total).toBeLessThan(manyRes.results.length);
  });
});

describe("binning", () => {
  it("bins only below 1 px per nucleotide", () => {
    expect(shouldBin(0.5)).toBe(true);
    expect(shouldBin(1)).toBe(false);
    expect(shouldBin(7)).toBe(false);
  });

  it("aggregates sites into BIN_PX-wide bins with count and max probability", () => {
    const n = 10_000;
    const s = makeScale(fullView(n), 64, 1064); // 0.1 px per nt => 20 nt per 2-px bin
    // Bins are aligned to the pixel grid: the bin starting at px 74 covers positions 101..120.
    const sites = [site(101, "m6A", 0.2), site(105, "m6A", 0.9), site(119, "m6A", 0.4), site(5000, "m6A", 0.7)];
    const bins = binSites(sites, s, BIN_PX);
    expect(bins).toHaveLength(2);
    expect(bins[0]).toMatchObject({ count: 3, maxProb: 0.9, w: BIN_PX });
    expect(bins[0].start).toBeLessThanOrEqual(101);
    expect(bins[0].end).toBeGreaterThanOrEqual(119);
    expect(bins[1]).toMatchObject({ count: 1, maxProb: 0.7 });
    expect(bins[1].x).toBeGreaterThan(bins[0].x);
    expect(binSites([], s)).toEqual([]);
  });

  it("never produces more bins than pixel columns / BIN_PX", () => {
    const n = 10_000;
    const width = 1000;
    const s = makeScale(fullView(n), 0, width);
    const dense = Array.from({ length: 3000 }, (_, i) => site(26 + Math.floor((i * 9949) / 3000), "m6A", (i % 10) / 10));
    const bins = binSites(dense, s, BIN_PX);
    expect(bins.length).toBeLessThanOrEqual(width / BIN_PX);
    expect(bins.reduce((a, b) => a + b.count, 0)).toBe(3000);
  });

  it("overviewBins yields at most one entry per pixel column", () => {
    const bins = overviewBins(manyRes.results, manyRes.meta.sequence_length, 300);
    expect(bins.length).toBeLessThanOrEqual(300);
    expect(bins.every((b) => b.x >= 0 && b.x < 300 && b.count >= 1)).toBe(true);
    expect(overviewBins([], 100, 300)).toEqual([]);
  });
});

describe("glyph encoding and nucleotides", () => {
  it("height grows with probability, with a visible floor", () => {
    expect(glyphHeightFrac(0)).toBeCloseTo(0.3);
    expect(glyphHeightFrac(1)).toBeCloseTo(1);
    expect(glyphHeightFrac(0.5)).toBeGreaterThan(glyphHeightFrac(0.2));
    expect(glyphWidth(0.5)).toBe(3);
    expect(glyphWidth(10)).toBe(9);
  });

  it("shows T as U and flags a base mismatch", () => {
    expect(letterAt("ACGT", 4)).toBe("U");
    expect(letterAt("ACGT", 5)).toBeNull();
    expect(letterAt(null, 1)).toBeNull();
    expect(baseMismatch("U", "Psi")).toBe(false);
    expect(baseMismatch("T", "Um")).toBe(false);
    expect(baseMismatch("A", "m5C")).toBe(true);
    expect(baseMismatch(null, "m5C")).toBe(false);
  });

  it("parses the position out of a site key", () => {
    expect(parseKeyPosition("52:Gm")).toBe(52);
    expect(parseKeyPosition("nope")).toBeNull();
  });
});
