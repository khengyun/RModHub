/**
 * Hand-written SVG track of the predicted modification sites (no chart library, no
 * external assets; safe under `default-src 'self'`).
 *
 * Layout (top to bottom): overview strip (all sites over the full length + viewport box),
 * ruler with nice ticks, nucleotide letters (only when <= LETTERS_MAX_SPAN nt are visible),
 * the 51-nt window bracket of the active site, then one lane per modification type.
 *
 * Encoding: glyph colour = modification type, glyph height = model probability.
 * Zoom: Ctrl/Cmd + wheel (or pinch) around the cursor, +/- buttons, click a density bin;
 * pan: drag, Shift + wheel, horizontal wheel, arrow keys. Plain wheel scrolls the page.
 * Density: below BIN_MIN_PX_PER_NT px per nucleotide, sites are aggregated per lane into
 * BIN_PX-wide bins (click a bin to zoom in). Only glyphs inside the viewport are rendered.
 *
 * The props are the contract with SequencePage (and the future signal-branch result page).
 */
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import {
  siteKey,
  type ModSite,
  type PredictionMeta,
  type SiteAttention,
} from "../../api/types";
import { modTypeInfo } from "../../lib/modTypes";
import { formatP, formatProb } from "../../lib/format";
import {
  BIN_PX,
  FALLBACK_WIDTH,
  LANE_H,
  LEFT_PAD,
  LETTERS_MAX_SPAN,
  RIGHT_PAD,
  WINDOW_HALF,
  baseMismatch,
  binSites,
  centerView,
  cullMargin,
  fullView,
  glyphHeightFrac,
  glyphWidth,
  groupByType,
  isFullView,
  laneTypes,
  letterAt,
  makeScale,
  overviewBins,
  panView,
  parseKeyPosition,
  shouldBin,
  spanOf,
  spanView,
  ticksFor,
  visiblePositions,
  visibleSlice,
  zoomView,
  type Bin,
  type View,
} from "./trackModel";

export interface TrackViewProps {
  /** Normalised ACGT sequence (client-side), or null when unavailable / length mismatch. */
  sequence: string | null;
  meta: PredictionMeta;
  /** Sites to draw — already filtered by the results table. */
  sites: ModSite[];
  /** Attention windows for ALL sites, keyed by siteKey(site). Empty map if not requested. */
  attentionByKey: Map<string, SiteAttention>;
  selectedKey: string | null;
  onSelect: (key: string | null) => void;
}

/* ---------- vertical layout (px) ---------- */
const OVERVIEW_Y = 6;
const OVERVIEW_H = 12;
const AXIS_Y = 44; // ruler baseline
const LETTERS_Y = 52;
const LETTERS_H = 15;
const BRACKET_Y = 70;
const LANES_Y = 84;
const BOTTOM_PAD = 8;

/** Opacity of the top-3 attention windows, best first. */
const ATTENTION_OPACITY = [0.4, 0.24, 0.14];
const LETTER_COLORS: Record<string, string> = {
  A: "#0f766e",
  C: "#1d4ed8",
  G: "#b45309",
  U: "#b91c1c",
};

interface TooltipState {
  x: number;
  y: number;
  content: ReactNode;
}

export function TrackView({
  sequence,
  meta,
  sites,
  attentionByKey,
  selectedKey,
  onSelect,
}: TrackViewProps) {
  const n = meta.sequence_length;
  const wrapperRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const hatchId = useId();
  const clipId = useId();
  const clip = `url(#${clipId})`;

  /* ----- container width (ResizeObserver; jsdom fallback) ----- */
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  useLayoutEffect(() => {
    const el = wrapperRef.current;
    if (!el) return;
    const measure = () => {
      const w = el.getBoundingClientRect?.().width ?? 0;
      setWidth(w > 100 ? Math.floor(w) : FALLBACK_WIDTH);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  /* ----- viewport ----- */
  const [view, setView] = useState<View>(() => fullView(n));
  const viewRef = useRef(view);
  viewRef.current = view;
  const widthRef = useRef(width);
  widthRef.current = width;
  // A new prediction (new meta object) resets the zoom.
  useEffect(() => setView(fullView(meta.sequence_length)), [meta]);

  const x0 = LEFT_PAD;
  const x1 = Math.max(x0 + 50, width - RIGHT_PAD);
  const scale = useMemo(() => makeScale(view, x0, x1), [view, x0, x1]);
  const { pxPerNt } = scale;

  /* ----- derived data ----- */
  const lanes = useMemo(() => laneTypes(sites), [sites]);
  const byType = useMemo(() => groupByType(sites), [sites]);
  const siteByKey = useMemo(() => {
    const m = new Map<string, ModSite>();
    for (const s of sites) m.set(siteKey(s), s);
    return m;
  }, [sites]);
  const laneIndex = useMemo(
    () => new Map(lanes.map((t, i) => [t, i])),
    [lanes],
  );
  const lanesH = Math.max(1, lanes.length) * LANE_H;
  const height = LANES_Y + lanesH + BOTTOM_PAD;
  const binned = shouldBin(pxPerNt);

  const { first: firstVisible, last: lastVisible } = visiblePositions(view);
  const margin = cullMargin(pxPerNt);
  const lanesContent = useMemo(() => {
    const lo = firstVisible - margin;
    const hi = lastVisible + margin;
    return lanes.map((type) => {
      const vis = visibleSlice(byType.get(type) ?? [], lo, hi);
      return {
        type,
        sites: binned ? [] : vis,
        bins: binned ? binSites(vis, scale, BIN_PX) : [],
      };
    });
    // `scale` changes with the view; recomputing per pan step is O(log n + visible).
  }, [lanes, byType, firstVisible, lastVisible, margin, binned, scale]);

  const ticks = useMemo(() => ticksFor(view, n, pxPerNt), [view, n, pxPerNt]);
  const overview = useMemo(
    () => overviewBins(sites, n, x1 - x0),
    [sites, n, x0, x1],
  );
  const showLetters = sequence !== null && spanOf(view) <= LETTERS_MAX_SPAN;

  /* ----- hover / selection / tooltip ----- */
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const activeKey =
    hoverKey && siteByKey.has(hoverKey) ? hoverKey : selectedKey;
  const activeSite = activeKey ? (siteByKey.get(activeKey) ?? null) : null;
  const activeAttention = activeKey
    ? (attentionByKey.get(activeKey) ?? null)
    : null;
  const activePosition =
    activeSite?.position ?? (activeKey ? parseKeyPosition(activeKey) : null);

  // When the selection comes from outside (table row click), make the site visible.
  useEffect(() => {
    if (!selectedKey) return;
    const pos = parseKeyPosition(selectedKey);
    if (pos === null) return;
    setView((v) => {
      const s = makeScale(
        v,
        LEFT_PAD,
        Math.max(LEFT_PAD + 50, widthRef.current - RIGHT_PAD),
      );
      if (shouldBin(s.pxPerNt)) return spanView(pos, LETTERS_MAX_SPAN + 1, n);
      if (pos < v.start + 1 || pos > v.end - 1) return centerView(v, pos, n);
      return v;
    });
  }, [selectedKey, n]);

  const wrapperPoint = useCallback((clientX: number, clientY: number) => {
    const r = wrapperRef.current?.getBoundingClientRect?.();
    // jsdom may deliver synthetic events without coordinates.
    const cx = Number.isFinite(clientX) ? clientX : 0;
    const cy = Number.isFinite(clientY) ? clientY : 0;
    return { x: cx - (r?.left ?? 0), y: cy - (r?.top ?? 0) };
  }, []);

  const siteTooltip = useCallback(
    (site: ModSite): ReactNode => {
      const info = modTypeInfo(site.mod_type);
      const letter = letterAt(sequence, site.position);
      const mismatch = baseMismatch(letter, site.mod_type);
      return (
        <div className="space-y-0.5">
          <div>
            <span
              className="inline-block h-2.5 w-2.5 rounded-sm align-middle"
              style={{ backgroundColor: info.color }}
              aria-hidden
            />{" "}
            <b>{info.label}</b> at position{" "}
            <b>{site.position.toLocaleString("en-US")}</b>
          </div>
          <div>
            probability {formatProb(site.probability)} · p-value{" "}
            {formatP(site.p_value)}
            {site.coverage !== null ? ` · coverage ${site.coverage}` : ""}
          </div>
          <div>
            nucleotide: <span className="font-mono">{letter ?? "n/a"}</span>
            {letter ? ` (target base of ${info.label}: ${info.base})` : ""}
          </div>
          {mismatch && (
            <div className="text-amber-700">
              note: base differs from the canonical target of this modification
            </div>
          )}
          <div className="text-slate-400">
            {attentionByKey.has(siteKey(site))
              ? "Attention windows highlighted · "
              : ""}
            click to {selectedKey === siteKey(site) ? "deselect" : "select"}
          </div>
        </div>
      );
    },
    [sequence, attentionByKey, selectedKey],
  );

  const binTooltip = (bin: Bin, type: string): ReactNode => (
    <div className="space-y-0.5">
      <div>
        <b>{modTypeInfo(type).label}</b>: {bin.count} site
        {bin.count === 1 ? "" : "s"} in positions{" "}
        {bin.start.toLocaleString("en-US")}–{bin.end.toLocaleString("en-US")}
      </div>
      <div>max probability {formatProb(bin.maxProb)}</div>
      <div className="text-slate-400">click to zoom in</div>
    </div>
  );

  const flankTooltip = (
    <div className="max-w-xs">
      <b>Not scored.</b> MultiRM scores the centre of a 51-nt window, so the
      first and last {WINDOW_HALF} nt of the input cannot receive a prediction.
    </div>
  );

  /* ----- glyph events (delegated on the lanes group) ----- */
  const dragRef = useRef<{
    x: number;
    view: View;
    pxPerNt: number;
    moved: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);

  const targetGlyph = (e: { target: EventTarget }) => {
    const el =
      e.target instanceof Element
        ? e.target.closest<SVGElement>("[data-key],[data-bin]")
        : null;
    return el;
  };

  const showTooltipAt = (
    clientX: number,
    clientY: number,
    content: ReactNode,
  ) => {
    const p = wrapperPoint(clientX, clientY);
    setTooltip({ x: p.x, y: p.y, content });
  };

  const onLanesPointerOver = (e: ReactPointerEvent<SVGGElement>) => {
    const el = targetGlyph(e);
    if (!el) return;
    const key = el.getAttribute("data-key");
    if (key) {
      const site = siteByKey.get(key);
      if (!site) return;
      setHoverKey(key);
      showTooltipAt(e.clientX, e.clientY, siteTooltip(site));
      return;
    }
    const binRef = el.getAttribute("data-bin");
    if (binRef) {
      const [type, idx] = binRef.split("#");
      const lane = lanesContent.find((l) => l.type === type);
      const bin = lane?.bins[Number(idx)];
      if (bin) showTooltipAt(e.clientX, e.clientY, binTooltip(bin, type));
    }
  };

  const onLanesPointerMove = (e: ReactPointerEvent<SVGGElement>) => {
    if (!tooltip || dragRef.current?.moved) return;
    const p = wrapperPoint(e.clientX, e.clientY);
    setTooltip((t) => (t ? { ...t, x: p.x, y: p.y } : t));
  };

  const onLanesPointerOut = (e: ReactPointerEvent<SVGGElement>) => {
    if (!targetGlyph(e)) return;
    setHoverKey(null);
    setTooltip(null);
  };

  const onLanesClick = (e: ReactMouseEvent<SVGGElement>) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    const el = targetGlyph(e);
    if (!el) return;
    const key = el.getAttribute("data-key");
    if (key) {
      onSelect(selectedKey === key ? null : key);
      return;
    }
    const binRef = el.getAttribute("data-bin");
    if (binRef) {
      const [type, idx] = binRef.split("#");
      const bin = lanesContent.find((l) => l.type === type)?.bins[Number(idx)];
      if (bin) {
        const center = (bin.start + bin.end) / 2;
        setView((v) => spanView(center, Math.max(20, spanOf(v) / 4), n));
        setTooltip(null);
      }
    }
  };

  const onLanesFocus = (e: ReactFocusEvent<SVGGElement>) => {
    const el = targetGlyph(e);
    const key = el?.getAttribute("data-key");
    if (!key) return;
    const site = siteByKey.get(key);
    if (!site) return;
    setHoverKey(key);
    // Anchor the tooltip on the glyph itself (keyboard users have no pointer).
    const svgRect = svgRef.current?.getBoundingClientRect?.();
    const wrapRect = wrapperRef.current?.getBoundingClientRect?.();
    const dx = (svgRect?.left ?? 0) - (wrapRect?.left ?? 0);
    const dy = (svgRect?.top ?? 0) - (wrapRect?.top ?? 0);
    const lane = laneIndex.get(site.mod_type) ?? 0;
    setTooltip({
      x: dx + scale.toPx(site.position),
      y: dy + LANES_Y + lane * LANE_H + LANE_H,
      content: siteTooltip(site),
    });
  };

  const onLanesBlur = (e: ReactFocusEvent<SVGGElement>) => {
    if (!targetGlyph(e)) return;
    setHoverKey(null);
    setTooltip(null);
  };

  /* ----- zoom / pan ----- */
  const zoomBy = useCallback(
    (factor: number) =>
      setView((v) => zoomView(v, factor, (v.start + v.end) / 2, n)),
    [n],
  );
  const fit = useCallback(() => setView(fullView(n)), [n]);

  // Native listener: React registers `wheel` as passive, so preventDefault() needs this.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      const zoom = e.ctrlKey || e.metaKey;
      const pan =
        !zoom && (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY));
      if (!zoom && !pan) return; // plain wheel: let the page scroll
      e.preventDefault();
      const v = viewRef.current;
      const sc = makeScale(
        v,
        LEFT_PAD,
        Math.max(LEFT_PAD + 50, widthRef.current - RIGHT_PAD),
      );
      if (zoom) {
        const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
        const factor = Math.exp(-Math.max(-200, Math.min(200, dy)) * 0.0025);
        const rect = svg.getBoundingClientRect();
        setView(zoomView(v, factor, sc.toNt(e.clientX - rect.left), n));
      } else {
        const d = e.deltaX !== 0 ? e.deltaX : e.deltaY;
        setView(panView(v, d / sc.pxPerNt, n));
      }
      setTooltip(null);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [n]);

  const onSvgPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (e.button !== 0) return;
    suppressClickRef.current = false;
    dragRef.current = {
      x: e.clientX,
      view: viewRef.current,
      pxPerNt,
      moved: false,
    };
  };
  const onSvgPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    if (!d.moved && Math.abs(dx) > 3) {
      d.moved = true;
      setTooltip(null);
      e.currentTarget.setPointerCapture?.(e.pointerId);
    }
    if (d.moved) setView(panView(d.view, -dx / d.pxPerNt, n));
  };
  const onSvgPointerUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    const d = dragRef.current;
    if (!d) return;
    suppressClickRef.current = d.moved;
    dragRef.current = null;
    if (d.moved && e.currentTarget.hasPointerCapture?.(e.pointerId))
      e.currentTarget.releasePointerCapture(e.pointerId);
  };

  const onSvgKeyDown = (e: ReactKeyboardEvent<SVGSVGElement>) => {
    const glyph = targetGlyph(e);
    const key = glyph?.getAttribute("data-key");
    if (key && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      onSelect(selectedKey === key ? null : key);
      return;
    }
    const step = spanOf(view) * 0.1;
    switch (e.key) {
      case "ArrowLeft":
        e.preventDefault();
        setView((v) => panView(v, -step, n));
        break;
      case "ArrowRight":
        e.preventDefault();
        setView((v) => panView(v, step, n));
        break;
      case "+":
      case "=":
        e.preventDefault();
        zoomBy(2);
        break;
      case "-":
        e.preventDefault();
        zoomBy(0.5);
        break;
      case "0":
        e.preventDefault();
        fit();
        break;
      case "Escape":
        if (selectedKey) onSelect(null);
        break;
    }
  };

  const onOverviewClick = (e: ReactMouseEvent<SVGRectElement>) => {
    const rect = svgRef.current?.getBoundingClientRect?.();
    const px = e.clientX - (rect?.left ?? 0);
    const pos = 0.5 + ((px - x0) / (x1 - x0)) * n;
    setView((v) =>
      centerView(
        isFullView(v, n) ? spanView(pos, Math.min(n, 200), n) : v,
        pos,
        n,
      ),
    );
  };

  /* ----- render helpers ----- */
  const clipX = (u: number) => Math.min(x1, Math.max(x0, scale.toPx(u)));
  const scoredX0 = clipX(meta.predicted_start - 0.5);
  const scoredX1 = clipX(meta.predicted_end + 0.5);
  const leftFlankX1 = clipX(meta.predicted_start - 0.5);
  const rightFlankX0 = clipX(meta.predicted_end + 0.5);
  const gw = glyphWidth(pxPerNt);
  const lanesBottom = LANES_Y + lanesH;
  const rangeLabel = `${firstVisible.toLocaleString("en-US")}–${Math.min(n, lastVisible).toLocaleString("en-US")}`;
  const tooltipLeft = tooltip
    ? tooltip.x > width - 260
      ? tooltip.x - 12
      : tooltip.x + 12
    : 0;
  const tooltipFlip = tooltip ? tooltip.x > width - 260 : false;

  return (
    <div
      data-testid="track-view"
      className="rounded border border-slate-200 bg-white"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b border-slate-100 px-3 py-2 text-xs text-slate-600">
        <div>
          <span className="font-semibold text-slate-800">Track view</span>
          <span className="mx-1.5 text-slate-300">|</span>
          showing{" "}
          <span data-testid="track-range" className="tabular-nums">
            {rangeLabel}
          </span>{" "}
          of {n.toLocaleString("en-US")} nt
          <span className="mx-1.5 text-slate-300">|</span>
          {sites.length.toLocaleString("en-US")} site
          {sites.length === 1 ? "" : "s"} in {lanes.length} lane
          {lanes.length === 1 ? "" : "s"}
          {binned && (
            <span className="ml-1.5 rounded bg-amber-50 px-1 text-amber-800">
              density mode: zoom in for individual sites
            </span>
          )}
        </div>
        <div className="flex items-center gap-1" role="group" aria-label="Zoom">
          <ZoomButton
            testId="track-zoom-out"
            label="Zoom out"
            onClick={() => zoomBy(0.5)}
            disabled={isFullView(view, n)}
          >
            −
          </ZoomButton>
          <ZoomButton
            testId="track-zoom-in"
            label="Zoom in"
            onClick={() => zoomBy(2)}
            disabled={spanOf(view) <= 20}
          >
            +
          </ZoomButton>
          <ZoomButton
            testId="track-zoom-fit"
            label="Fit whole sequence"
            onClick={fit}
            disabled={isFullView(view, n)}
          >
            fit
          </ZoomButton>
        </div>
      </div>

      <div ref={wrapperRef} className="relative w-full select-none">
        <svg
          ref={svgRef}
          width={width}
          height={height}
          role="group"
          aria-label={`Modification site track, ${sites.length} sites over ${n} nucleotides`}
          tabIndex={0}
          className="block cursor-grab touch-pan-y font-sans outline-none focus-visible:ring-2 focus-visible:ring-brand-600/50 active:cursor-grabbing"
          onPointerDown={onSvgPointerDown}
          onPointerMove={onSvgPointerMove}
          onPointerUp={onSvgPointerUp}
          onPointerCancel={onSvgPointerUp}
          onKeyDown={onSvgKeyDown}
        >
          <defs>
            <pattern
              id={hatchId}
              width="6"
              height="6"
              patternUnits="userSpaceOnUse"
              patternTransform="rotate(45)"
            >
              <rect width="6" height="6" fill="#f1f5f9" />
              <line
                x1="0"
                y1="0"
                x2="0"
                y2="6"
                stroke="#cbd5e1"
                strokeWidth="1.5"
              />
            </pattern>
            {/* Plot area: glyphs within the culling margin must not bleed into the label gutter. */}
            <clipPath id={clipId}>
              <rect x={x0} y={0} width={x1 - x0} height={height} />
            </clipPath>
          </defs>

          {/* ---- overview strip ---- */}
          <g data-testid="track-overview">
            <rect
              x={x0}
              y={OVERVIEW_Y}
              width={x1 - x0}
              height={OVERVIEW_H}
              fill="#f8fafc"
              stroke="#e2e8f0"
            />
            {overview.map((b) => (
              <rect
                key={b.x}
                x={x0 + b.x}
                y={OVERVIEW_Y + 1}
                width={1}
                height={OVERVIEW_H - 2}
                fill={b.color}
                fillOpacity={Math.min(1, 0.45 + 0.2 * b.count)}
              />
            ))}
            {!isFullView(view, n) && (
              <rect
                x={x0 + ((view.start - 0.5) / n) * (x1 - x0)}
                y={OVERVIEW_Y - 1.5}
                width={Math.max(2, (spanOf(view) / n) * (x1 - x0))}
                height={OVERVIEW_H + 3}
                fill="#1f4e79"
                fillOpacity={0.12}
                stroke="#1f4e79"
                strokeWidth={1.2}
                rx={1}
                pointerEvents="none"
              />
            )}
            <text
              x={x0 - 6}
              y={OVERVIEW_Y + OVERVIEW_H - 2}
              textAnchor="end"
              fontSize={9}
              fill="#94a3b8"
            >
              overview
            </text>
            <rect
              x={x0}
              y={OVERVIEW_Y - 2}
              width={x1 - x0}
              height={OVERVIEW_H + 4}
              fill="transparent"
              className="cursor-pointer"
              onClick={onOverviewClick}
            >
              <title>Click to move the viewport here</title>
            </rect>
          </g>

          {/* ---- ruler ---- */}
          <g data-testid="track-ruler" pointerEvents="none">
            <line x1={x0} y1={AXIS_Y} x2={x1} y2={AXIS_Y} stroke="#94a3b8" />
            {scoredX1 > scoredX0 && (
              <line
                x1={scoredX0}
                y1={AXIS_Y}
                x2={scoredX1}
                y2={AXIS_Y}
                stroke="#1f4e79"
                strokeWidth={2.5}
              />
            )}
            {ticks.map((t) => {
              const x = scale.toPx(t.pos);
              // Keep edge labels (e.g. "10,000" at the far right) inside the SVG.
              const half = ((t.label?.length ?? 0) * 6) / 2;
              const anchor = x + half > width - 2 ? "end" : x - half < 2 ? "start" : "middle";
              const lx = anchor === "end" ? x + 3 : anchor === "start" ? x - 3 : x;
              return (
                <g key={t.pos}>
                  <line
                    x1={x}
                    y1={AXIS_Y}
                    x2={x}
                    y2={AXIS_Y + (t.major ? 6 : 3)}
                    stroke="#64748b"
                  />
                  {t.major && (
                    <text
                      x={lx}
                      y={AXIS_Y - 5}
                      textAnchor={anchor}
                      fontSize={10}
                      fill="#475569"
                    >
                      {t.label}
                    </text>
                  )}
                </g>
              );
            })}
          </g>

          {/* ---- nucleotide letters ---- */}
          {showLetters && (
            <g
              data-testid="track-letters"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
              pointerEvents="none"
              clipPath={clip}
            >
              {Array.from(
                {
                  length: Math.max(
                    0,
                    Math.min(n, lastVisible) - firstVisible + 1,
                  ),
                },
                (_, i) => {
                  const pos = firstVisible + i;
                  const letter = letterAt(sequence, pos);
                  if (!letter) return null;
                  return (
                    <text
                      key={pos}
                      x={scale.toPx(pos)}
                      y={LETTERS_Y + LETTERS_H - 3}
                      textAnchor="middle"
                      fontSize={Math.max(7, Math.min(13, pxPerNt * 1.2))}
                      fill={LETTER_COLORS[letter] ?? "#334155"}
                    >
                      {letter}
                    </text>
                  );
                },
              )}
            </g>
          )}

          {/* ---- lanes ---- */}
          <g
            onPointerOver={onLanesPointerOver}
            onPointerMove={onLanesPointerMove}
            onPointerOut={onLanesPointerOut}
            onClick={onLanesClick}
            onFocus={onLanesFocus}
            onBlur={onLanesBlur}
          >
            {/* scored band + flanks */}
            {scoredX1 > scoredX0 && (
              <rect
                x={scoredX0}
                y={LANES_Y}
                width={scoredX1 - scoredX0}
                height={lanesH}
                fill="#eef4fb"
                fillOpacity={0.55}
              />
            )}
            {leftFlankX1 > x0 && (
              <rect
                data-testid="track-flank"
                x={x0}
                y={LANES_Y}
                width={leftFlankX1 - x0}
                height={lanesH}
                fill={`url(#${hatchId})`}
                onPointerOver={(e) =>
                  showTooltipAt(e.clientX, e.clientY, flankTooltip)
                }
                onPointerOut={() => setTooltip(null)}
              />
            )}
            {rightFlankX0 < x1 && (
              <rect
                data-testid="track-flank"
                x={rightFlankX0}
                y={LANES_Y}
                width={x1 - rightFlankX0}
                height={lanesH}
                fill={`url(#${hatchId})`}
                onPointerOver={(e) =>
                  showTooltipAt(e.clientX, e.clientY, flankTooltip)
                }
                onPointerOut={() => setTooltip(null)}
              />
            )}
            {leftFlankX1 - x0 > 44 && (
              <text
                x={x0 + 4}
                y={LANES_Y + 11}
                fontSize={9}
                fill="#64748b"
                pointerEvents="none"
              >
                not scored
              </text>
            )}
            {x1 - rightFlankX0 > 44 && (
              <text
                x={x1 - 4}
                y={LANES_Y + 11}
                fontSize={9}
                fill="#64748b"
                textAnchor="end"
                pointerEvents="none"
              >
                not scored
              </text>
            )}

            {/* attention windows of the active site (behind the glyphs) */}
            {activeAttention && activeSite && (
              <g
                data-testid="track-attention-layer"
                pointerEvents="none"
                clipPath={clip}
              >
                {activeAttention.windows.slice(0, 3).map((w, i) => {
                  const wx0 = clipX(w.start - 0.5);
                  const wx1 = clipX(w.end + 0.5);
                  if (wx1 <= wx0) return null;
                  return (
                    <rect
                      key={`${w.start}-${w.end}`}
                      data-testid="track-attention"
                      data-rank={i + 1}
                      x={wx0}
                      y={LANES_Y}
                      width={wx1 - wx0}
                      height={lanesH}
                      fill={modTypeInfo(activeSite.mod_type).color}
                      fillOpacity={ATTENTION_OPACITY[i] ?? 0.1}
                      stroke={
                        i === 0
                          ? modTypeInfo(activeSite.mod_type).color
                          : "none"
                      }
                      strokeOpacity={0.7}
                    />
                  );
                })}
              </g>
            )}

            {/* 51-nt window bracket + position guide of the active site */}
            {activePosition !== null && (
              <g
                data-testid="track-window"
                pointerEvents="none"
                clipPath={clip}
              >
                {(() => {
                  const bx0 = clipX(activePosition - WINDOW_HALF - 0.5);
                  const bx1 = clipX(activePosition + WINDOW_HALF + 0.5);
                  const cx = scale.toPx(activePosition);
                  const inView = cx >= x0 && cx <= x1;
                  return (
                    <>
                      {bx1 > bx0 && (
                        <path
                          d={`M${bx0},${BRACKET_Y + 8} V${BRACKET_Y + 2} H${bx1} V${BRACKET_Y + 8}`}
                          fill="none"
                          stroke="#334155"
                          strokeWidth={1}
                        />
                      )}
                      {bx1 - bx0 > 70 && (
                        <text
                          x={(bx0 + bx1) / 2}
                          y={BRACKET_Y + 9}
                          textAnchor="middle"
                          fontSize={9}
                          fill="#334155"
                        >
                          51-nt window
                        </text>
                      )}
                      {inView && (
                        <line
                          x1={cx}
                          y1={BRACKET_Y + 2}
                          x2={cx}
                          y2={lanesBottom}
                          stroke="#334155"
                          strokeDasharray="2 3"
                          strokeOpacity={0.7}
                        />
                      )}
                    </>
                  );
                })()}
              </g>
            )}

            {/* lane rows */}
            {lanes.length === 0 && (
              <g data-testid="track-empty">
                <rect
                  x={x0}
                  y={LANES_Y}
                  width={x1 - x0}
                  height={lanesH}
                  fill="none"
                  stroke="#e2e8f0"
                />
                <text
                  x={(x0 + x1) / 2}
                  y={LANES_Y + LANE_H / 2 + 4}
                  textAnchor="middle"
                  fontSize={12}
                  fill="#94a3b8"
                >
                  No sites to display for the current filters
                </text>
              </g>
            )}
            {lanesContent.map((lane, li) => {
              const info = modTypeInfo(lane.type);
              const y = LANES_Y + li * LANE_H;
              const inner = LANE_H - 4;
              return (
                <g
                  key={lane.type}
                  data-testid="track-lane"
                  data-mod-type={lane.type}
                  data-lane-index={li}
                >
                  <line
                    x1={x0}
                    y1={y + LANE_H}
                    x2={x1}
                    y2={y + LANE_H}
                    stroke="#e2e8f0"
                    pointerEvents="none"
                  />
                  <text
                    x={x0 - 8}
                    y={y + LANE_H / 2 + 4}
                    textAnchor="end"
                    fontSize={11}
                    fontWeight={600}
                    fill={info.color}
                    pointerEvents="none"
                  >
                    {info.label}
                  </text>
                  <g clipPath={clip}>
                    {lane.sites.map((site) => {
                      const key = siteKey(site);
                      const selected = key === selectedKey;
                      const hovered = key === hoverKey;
                      const h =
                        Math.round(
                          inner * glyphHeightFrac(site.probability) * 10,
                        ) / 10;
                      const cx = scale.toPx(site.position);
                      return (
                        <rect
                          key={key}
                          data-testid="track-site"
                          data-key={key}
                          data-selected={selected ? "true" : "false"}
                          role="button"
                          tabIndex={0}
                          aria-label={`${info.label} at position ${site.position}, probability ${formatProb(site.probability)}, p-value ${formatP(site.p_value)}`}
                          aria-pressed={selected}
                          x={cx - gw / 2}
                          y={y + LANE_H - 2 - h}
                          width={gw}
                          height={h}
                          rx={Math.min(2, gw / 2)}
                          fill={info.color}
                          fillOpacity={selected || hovered ? 1 : 0.8}
                          stroke={
                            selected ? "#0f172a" : hovered ? "#475569" : "none"
                          }
                          strokeWidth={selected ? 1.5 : 1}
                          className="cursor-pointer outline-none"
                        />
                      );
                    })}
                    {lane.bins.map((bin, bi) => {
                      const h =
                        Math.round(inner * glyphHeightFrac(bin.maxProb) * 10) /
                        10;
                      return (
                        <rect
                          key={bin.x}
                          data-testid="track-bin"
                          data-bin={`${lane.type}#${bi}`}
                          data-count={bin.count}
                          role="button"
                          tabIndex={-1}
                          aria-label={`${info.label}: ${bin.count} sites in positions ${bin.start}-${bin.end}; click to zoom in`}
                          x={bin.x}
                          y={y + LANE_H - 2 - h}
                          width={bin.w}
                          height={h}
                          fill={info.color}
                          fillOpacity={Math.min(1, 0.45 + 0.15 * bin.count)}
                          className="cursor-zoom-in"
                        />
                      );
                    })}
                  </g>
                </g>
              );
            })}
          </g>
        </svg>

        {tooltip && (
          <div
            data-testid="track-tooltip"
            role="tooltip"
            className="pointer-events-none absolute z-10 max-w-xs rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-700 shadow-md"
            style={{
              left: tooltipLeft,
              top: tooltip.y + 14,
              transform: tooltipFlip ? "translateX(-100%)" : undefined,
            }}
          >
            {tooltip.content}
          </div>
        )}
      </div>

      <p
        data-testid="track-legend"
        className="border-t border-slate-100 px-3 py-1.5 text-[11px] leading-4 text-slate-500"
      >
        Colour = modification type; glyph height = model probability (taller =
        more confident); one lane per type, so sites at the same position stay
        distinct.{" "}
        {attentionByKey.size > 0
          ? "Highlighted = regions the model attended to most when scoring this site (top 3); the bracket marks its 51-nt scoring window."
          : "Attention windows were not requested for this run."}{" "}
        Hatched ends = not scored (first/last {WINDOW_HALF} nt). Ctrl/⌘ + wheel
        or +/− zooms, drag or Shift + wheel pans, click a site to select it
        (Enter/Space with the keyboard); letters appear below the ruler when ≤{" "}
        {LETTERS_MAX_SPAN} nt are visible (T shown as U). When fewer than 1 px
        per nucleotide is available, sites are merged into {BIN_PX}-px density
        bins — click a bin to zoom in.
      </p>
    </div>
  );
}

function ZoomButton({
  testId,
  label,
  onClick,
  disabled,
  children,
}: {
  testId: string;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className="min-w-7 rounded border border-slate-300 bg-white px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}
