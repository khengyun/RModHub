/**
 * Pure geometry for the SVG track view: nucleotide <-> pixel scale, viewport zoom/pan,
 * "nice" axis ticks, lane assignment, visible-range culling and density binning.
 * No DOM, no React: everything here is unit-tested in trackModel.test.ts.
 *
 * Coordinate system: a nucleotide at 1-based position p occupies the continuous interval
 * [p - 0.5, p + 0.5]; the whole sequence is [0.5, N + 0.5]. A `View` is the visible part
 * of that interval.
 */
import { MOD_TYPES, type ModSite } from "../../api/types";
import { modTypeInfo } from "../../lib/modTypes";

export interface View {
  start: number;
  end: number;
}

/* ---------- layout constants (px) ---------- */

/** Left gutter for the lane labels. */
export const LEFT_PAD = 64;
export const RIGHT_PAD = 12;
export const LANE_H = 22;
/** Width used when the container cannot be measured (jsdom, hidden tab). */
export const FALLBACK_WIDTH = 1000;
/** Never show fewer nucleotides than this (max zoom). */
export const MIN_SPAN_NT = 20;
/** Letters are drawn under the axis when at most this many nucleotides are visible. */
export const LETTERS_MAX_SPAN = 150;
/** Below this pixel density, sites are aggregated into density bins of BIN_PX pixels. */
export const BIN_MIN_PX_PER_NT = 1;
export const BIN_PX = 2;
/** MultiRM scores the centre of a 51-nt window. */
export const WINDOW_HALF = 25;

/* ---------- viewport ---------- */

export function fullView(n: number): View {
  return { start: 0.5, end: n + 0.5 };
}

export function spanOf(v: View): number {
  return v.end - v.start;
}

/** Keep the view inside [0.5, N + 0.5] and its span within [MIN_SPAN_NT, N]. */
export function clampView(v: View, n: number): View {
  const lo = 0.5;
  const hi = n + 0.5;
  let span = Math.min(Math.max(v.end - v.start, Math.min(MIN_SPAN_NT, n)), n);
  if (!Number.isFinite(span) || span <= 0) span = n;
  let start = v.start;
  if (start < lo) start = lo;
  if (start + span > hi) start = hi - span;
  return { start, end: start + span };
}

/** factor > 1 zooms in; `anchor` (continuous nt coordinate) stays under the cursor. */
export function zoomView(v: View, factor: number, anchor: number, n: number): View {
  const span = spanOf(v);
  const newSpan = span / factor;
  const t = span === 0 ? 0.5 : (anchor - v.start) / span; // relative position of the anchor
  const start = anchor - t * newSpan;
  return clampView({ start, end: start + newSpan }, n);
}

export function panView(v: View, deltaNt: number, n: number): View {
  return clampView({ start: v.start + deltaNt, end: v.end + deltaNt }, n);
}

/** Same span, centred on `pos`. */
export function centerView(v: View, pos: number, n: number): View {
  const span = spanOf(v);
  return clampView({ start: pos - span / 2, end: pos + span / 2 }, n);
}

/** A window of `span` nucleotides centred on `pos`. */
export function spanView(pos: number, span: number, n: number): View {
  return clampView({ start: pos - span / 2, end: pos + span / 2 }, n);
}

export function isFullView(v: View, n: number): boolean {
  return v.start <= 0.5 + 1e-9 && v.end >= n + 0.5 - 1e-9;
}

/** Whole positions that are (at least partly) visible. */
export function visiblePositions(v: View): { first: number; last: number } {
  return { first: Math.max(1, Math.ceil(v.start)), last: Math.floor(v.end) };
}

/* ---------- scale ---------- */

export interface Scale {
  view: View;
  x0: number;
  x1: number;
  pxPerNt: number;
  /** Continuous nt coordinate -> px (position p is centred at toPx(p)). */
  toPx: (u: number) => number;
  /** px -> continuous nt coordinate. */
  toNt: (px: number) => number;
}

export function makeScale(view: View, x0: number, x1: number): Scale {
  const span = Math.max(spanOf(view), 1e-9);
  const pxPerNt = (x1 - x0) / span;
  return {
    view,
    x0,
    x1,
    pxPerNt,
    toPx: (u) => x0 + (u - view.start) * pxPerNt,
    toNt: (px) => view.start + (px - x0) / pxPerNt,
  };
}

/* ---------- axis ticks ---------- */

const STEPS = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000];

/** Smallest "nice" step whose labels are at least `minPx` apart. */
export function niceStep(pxPerNt: number, minPx = 55): number {
  for (const s of STEPS) if (s * pxPerNt >= minPx) return s;
  return STEPS[STEPS.length - 1];
}

export interface Tick {
  pos: number;
  /** Major ticks carry a label. */
  major: boolean;
  label: string | null;
}

/**
 * Major ticks at multiples of the nice step, minor ticks in between when there is room,
 * plus position 1 and N as labelled end ticks whenever they are visible. A regular tick
 * that would collide with an end label is dropped.
 */
export function ticksFor(view: View, n: number, pxPerNt: number, minPx = 55): Tick[] {
  const step = niceStep(pxPerNt, minPx);
  const { first, last } = visiblePositions(view);
  const ticks: Tick[] = [];
  const minorStep = step >= 10 ? step / 5 : step >= 2 ? step / 2 : 0;
  const minorOk = minorStep > 0 && minorStep * pxPerNt >= 8;

  const endLabelPx = 0.55 * minPx; // labels closer than this to "1"/"N" would overlap
  const ends: number[] = [];
  if (first <= 1 && 1 <= last) ends.push(1);
  if (first <= n && n <= last && n !== 1) ends.push(n);

  const collides = (p: number) => ends.some((e) => e !== p && Math.abs(e - p) * pxPerNt < endLabelPx);

  const fmt = (p: number) => p.toLocaleString("en-US");
  for (const e of ends) ticks.push({ pos: e, major: true, label: fmt(e) });

  const from = Math.max(first, 1);
  const to = Math.min(last, n);
  const firstMajor = Math.ceil(from / step) * step;
  for (let p = firstMajor; p <= to; p += step) {
    if (p === 1 || p === n) continue;
    if (collides(p)) continue;
    ticks.push({ pos: p, major: true, label: fmt(p) });
  }
  if (minorOk) {
    const firstMinor = Math.ceil(from / minorStep) * minorStep;
    for (let p = firstMinor; p <= to; p += minorStep) {
      if (p % step === 0 || p === 1 || p === n) continue;
      ticks.push({ pos: p, major: false, label: null });
    }
  }
  ticks.sort((a, b) => a.pos - b.pos);
  return ticks;
}

/* ---------- lanes ---------- */

/**
 * One lane per modification type present in `sites`, in the canonical MOD_TYPES order;
 * unknown types (future models) are appended alphabetically.
 */
export function laneTypes(sites: ModSite[]): string[] {
  const present = new Set<string>();
  for (const s of sites) present.add(s.mod_type);
  const known = MOD_TYPES.filter((t) => present.has(t)) as string[];
  const unknown = [...present].filter((t) => !(MOD_TYPES as readonly string[]).includes(t)).sort();
  return [...known, ...unknown];
}

/** Sites grouped by type, each group sorted by position (needed for binary-search culling). */
export function groupByType(sites: ModSite[]): Map<string, ModSite[]> {
  const map = new Map<string, ModSite[]>();
  for (const s of sites) {
    let arr = map.get(s.mod_type);
    if (!arr) map.set(s.mod_type, (arr = []));
    arr.push(s);
  }
  for (const arr of map.values()) arr.sort((a, b) => a.position - b.position);
  return map;
}

/* ---------- culling & binning ---------- */

/** First index with position >= lo (binary search on a position-sorted array). */
export function lowerBound(sorted: ModSite[], lo: number): number {
  let a = 0;
  let b = sorted.length;
  while (a < b) {
    const m = (a + b) >> 1;
    if (sorted[m].position < lo) a = m + 1;
    else b = m;
  }
  return a;
}

/** Sites with lo <= position <= hi, from a position-sorted array. O(log n + k). */
export function visibleSlice(sorted: ModSite[], lo: number, hi: number): ModSite[] {
  const from = lowerBound(sorted, lo);
  const out: ModSite[] = [];
  for (let i = from; i < sorted.length && sorted[i].position <= hi; i++) out.push(sorted[i]);
  return out;
}

/** Culling margin in nucleotides: a glyph is a few px wide, so keep sites just off-screen. */
export function cullMargin(pxPerNt: number): number {
  return Math.max(1, Math.ceil(8 / pxPerNt));
}

export function shouldBin(pxPerNt: number): boolean {
  return pxPerNt < BIN_MIN_PX_PER_NT;
}

export interface Bin {
  /** First / last position covered by the bin (1-based, inclusive). */
  start: number;
  end: number;
  count: number;
  maxProb: number;
  /** Left edge in px and width in px. */
  x: number;
  w: number;
}

/**
 * Aggregate position-sorted visible sites into `binPx`-wide bins (density marks).
 * Only non-empty bins are returned.
 */
export function binSites(sortedVisible: ModSite[], scale: Scale, binPx = BIN_PX): Bin[] {
  const bins = new Map<number, Bin>();
  for (const s of sortedVisible) {
    const px = scale.toPx(s.position);
    const idx = Math.floor((px - scale.x0) / binPx);
    let bin = bins.get(idx);
    if (!bin) {
      const x = scale.x0 + idx * binPx;
      bin = {
        start: Math.max(1, Math.ceil(scale.toNt(x))),
        end: Math.max(1, Math.floor(scale.toNt(x + binPx) - 1e-9)),
        count: 0,
        maxProb: 0,
        x,
        w: binPx,
      };
      bins.set(idx, bin);
    }
    bin.count += 1;
    if (s.probability > bin.maxProb) bin.maxProb = s.probability;
    if (s.position < bin.start) bin.start = s.position;
    if (s.position > bin.end) bin.end = s.position;
  }
  return [...bins.values()].sort((a, b) => a.x - b.x);
}

/* ---------- overview strip ---------- */

export interface OverviewBin {
  /** Pixel column offset from the strip's left edge. */
  x: number;
  count: number;
  /** Colour of the highest-probability site in the column. */
  color: string;
}

/** One entry per non-empty 1-px column over the full sequence length. */
export function overviewBins(sites: ModSite[], n: number, widthPx: number): OverviewBin[] {
  if (widthPx <= 0 || n <= 0) return [];
  const cols = new Map<number, { count: number; best: number; color: string }>();
  const w = Math.max(1, Math.floor(widthPx));
  for (const s of sites) {
    const x = Math.min(w - 1, Math.max(0, Math.floor(((s.position - 0.5) / n) * w)));
    const c = cols.get(x);
    if (!c) cols.set(x, { count: 1, best: s.probability, color: modTypeInfo(s.mod_type).color });
    else {
      c.count += 1;
      if (s.probability > c.best) {
        c.best = s.probability;
        c.color = modTypeInfo(s.mod_type).color;
      }
    }
  }
  return [...cols.entries()]
    .map(([x, c]) => ({ x, count: c.count, color: c.color }))
    .sort((a, b) => a.x - b.x);
}

/* ---------- glyph encoding ---------- */

/** Glyph height as a fraction of the lane: taller = higher model probability (floor 0.3). */
export function glyphHeightFrac(probability: number): number {
  const p = Math.min(1, Math.max(0, probability));
  return 0.3 + 0.7 * p;
}

/** Glyph width: fills the nucleotide column (1 px gap) when zoomed in, else 3 px. */
export function glyphWidth(pxPerNt: number): number {
  return pxPerNt >= 4 ? pxPerNt - 1 : 3;
}

/* ---------- nucleotides ---------- */

/** Display letters as RNA (the client normalises U -> T; show T back as U). */
export function rnaLetter(ch: string): string {
  return ch === "T" ? "U" : ch;
}

export function letterAt(sequence: string | null, position: number): string | null {
  if (!sequence || position < 1 || position > sequence.length) return null;
  return rnaLetter(sequence[position - 1]);
}

/** True when the nucleotide at the site differs from the canonical target base of the type. */
export function baseMismatch(letter: string | null, modType: string): boolean {
  if (!letter) return false;
  return rnaLetter(letter) !== modTypeInfo(modType).base;
}

export function parseKeyPosition(key: string): number | null {
  const p = Number.parseInt(key, 10);
  return Number.isFinite(p) && p > 0 ? p : null;
}
