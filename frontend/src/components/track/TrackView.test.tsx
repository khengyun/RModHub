import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import golden from "../../api/fixtures/golden_attention.json";
import many from "../../api/fixtures/many_rows.json";
import { siteKey, type PredictResponse, type SiteAttention } from "../../api/types";
import { TrackView } from "./TrackView";

const goldenRes = golden as unknown as PredictResponse;
const manyFixture = many as unknown as { request: { sequence: string }; response: PredictResponse };

// Same 151-nt sample the golden fixture was scored on (see sample.json); only the letters
// under the sites matter for the tooltip assertions below.
const GOLDEN_SEQUENCE = "A".repeat(151);

function attentionMap(res: PredictResponse): Map<string, SiteAttention> {
  const map = new Map<string, SiteAttention>();
  for (const a of res.meta.attention ?? []) map.set(siteKey(a), a);
  return map;
}

function renderGolden(overrides: Partial<Parameters<typeof TrackView>[0]> = {}) {
  const onSelect = vi.fn();
  const utils = render(
    <TrackView
      sequence={GOLDEN_SEQUENCE}
      meta={goldenRes.meta}
      sites={goldenRes.results}
      attentionByKey={attentionMap(goldenRes)}
      selectedKey={null}
      onSelect={onSelect}
      {...overrides}
    />,
  );
  return { onSelect, ...utils };
}

describe("TrackView (golden fixture, 151 nt, 22 sites)", () => {
  it("renders one glyph per site and one lane per modification type", () => {
    renderGolden();
    const root = screen.getByTestId("track-view");
    expect(within(root).getAllByTestId("track-site")).toHaveLength(22);
    const lanes = within(root).getAllByTestId("track-lane");
    expect(lanes.map((l) => l.getAttribute("data-mod-type"))).toEqual(["Cm", "Gm", "Um", "m1A", "m5C", "m5U", "Psi"]);
    // Stacked sites at 123 are in three different lanes.
    for (const t of ["Um", "m5U", "Psi"]) {
      const lane = lanes.find((l) => l.getAttribute("data-mod-type") === t)!;
      expect(lane.querySelector(`[data-key="123:${t}"]`)).not.toBeNull();
    }
    expect(within(root).getByTestId("track-zoom-in")).toBeInTheDocument();
    expect(within(root).getByTestId("track-zoom-out")).toBeInTheDocument();
    expect(within(root).getByTestId("track-zoom-fit")).toBeInTheDocument();
    // Ruler labels adapt to the length: 1 … 151.
    const ruler = within(root).getByTestId("track-ruler");
    expect(ruler.textContent).toContain("1");
    expect(ruler.textContent).toContain("151");
    expect(ruler.textContent).toContain("100");
  });

  it("clicking a glyph selects it, clicking the selected glyph deselects", () => {
    const { onSelect, rerender } = renderGolden();
    const glyph = screen.getByTestId("track-view").querySelector('[data-key="52:Gm"]')!;
    expect(glyph.getAttribute("data-selected")).toBe("false");
    fireEvent.click(glyph);
    expect(onSelect).toHaveBeenCalledWith("52:Gm");

    rerender(
      <TrackView
        sequence={GOLDEN_SEQUENCE}
        meta={goldenRes.meta}
        sites={goldenRes.results}
        attentionByKey={attentionMap(goldenRes)}
        selectedKey="52:Gm"
        onSelect={onSelect}
      />,
    );
    const selected = screen.getByTestId("track-view").querySelector('[data-key="52:Gm"]')!;
    expect(selected.getAttribute("data-selected")).toBe("true");
    expect(selected.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(selected);
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("draws the attention windows of the selected site", () => {
    renderGolden({ selectedKey: "52:Gm" });
    const rects = screen.getAllByTestId("track-attention");
    expect(rects.length).toBeGreaterThanOrEqual(1);
    expect(rects.length).toBeLessThanOrEqual(3);
    expect(rects[0].getAttribute("data-rank")).toBe("1");
    expect(screen.getByTestId("track-window")).toBeInTheDocument();
  });

  it("draws no attention rects when nothing is selected, but does on hover", () => {
    renderGolden();
    expect(screen.queryAllByTestId("track-attention")).toHaveLength(0);
    const glyph = screen.getByTestId("track-view").querySelector('[data-key="63:m5C"]')!;
    fireEvent.pointerOver(glyph, { clientX: 300, clientY: 150 });
    expect(screen.getAllByTestId("track-attention").length).toBeGreaterThanOrEqual(1);
    fireEvent.pointerOut(glyph);
    expect(screen.queryAllByTestId("track-attention")).toHaveLength(0);
  });

  it("shows an HTML tooltip on hover with position, type, probability, p-value and nucleotide", () => {
    renderGolden();
    const glyph = screen.getByTestId("track-view").querySelector('[data-key="63:m5C"]')!;
    expect(screen.queryByTestId("track-tooltip")).toBeNull();
    fireEvent.pointerOver(glyph, { clientX: 300, clientY: 150 });
    const tip = screen.getByTestId("track-tooltip");
    expect(tip.tagName).toBe("DIV");
    expect(tip.textContent).toContain("m5C");
    expect(tip.textContent).toContain("63");
    expect(tip.textContent).toContain("0.532");
    expect(tip.textContent).toContain("0.0467");
    // The test sequence is all A, so an m5C site sits on a non-canonical base.
    expect(tip.textContent).toContain("nucleotide: A");
    expect(tip.textContent).toContain("base differs from the canonical target");
    fireEvent.pointerOut(glyph);
    expect(screen.queryByTestId("track-tooltip")).toBeNull();
  });

  it("does not warn about the base when the letter matches, and shows n/a without a sequence", () => {
    const { unmount } = renderGolden({ sequence: "C".repeat(151) });
    fireEvent.pointerOver(screen.getByTestId("track-view").querySelector('[data-key="63:m5C"]')!);
    expect(screen.getByTestId("track-tooltip").textContent).not.toContain("base differs");
    unmount();

    renderGolden({ sequence: null });
    fireEvent.pointerOver(screen.getByTestId("track-view").querySelector('[data-key="63:m5C"]')!);
    expect(screen.getByTestId("track-tooltip").textContent).toContain("nucleotide: n/a");
    expect(screen.queryByTestId("track-letters")).toBeNull();
  });

  it("glyphs are keyboard accessible: focusable buttons, Enter / Space select", () => {
    const { onSelect } = renderGolden();
    const glyph = screen.getByTestId("track-view").querySelector('[data-key="52:Gm"]')!;
    expect(glyph.getAttribute("role")).toBe("button");
    expect(glyph.getAttribute("tabindex")).toBe("0");
    expect(glyph.getAttribute("aria-label")).toMatch(/Gm at position 52/);
    fireEvent.focus(glyph);
    expect(screen.getByTestId("track-tooltip")).toBeInTheDocument();
    fireEvent.keyDown(glyph, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith("52:Gm");
    fireEvent.keyDown(glyph, { key: " " });
    expect(onSelect).toHaveBeenCalledTimes(2);
    fireEvent.blur(glyph);
    expect(screen.queryByTestId("track-tooltip")).toBeNull();
  });

  it("zoom buttons change the visible range; letters appear when <= 150 nt are visible", () => {
    renderGolden();
    const root = screen.getByTestId("track-view");
    expect(within(root).getByTestId("track-range").textContent).toBe("1–151");
    expect(within(root).queryByTestId("track-letters")).toBeNull(); // 151 > 150 at fit
    expect(within(root).getByTestId("track-zoom-fit")).toBeDisabled();

    fireEvent.click(within(root).getByTestId("track-zoom-in"));
    const range = within(root).getByTestId("track-range").textContent!;
    expect(range).not.toBe("1–151");
    const letters = within(root).getByTestId("track-letters");
    expect(letters.querySelectorAll("text").length).toBeGreaterThan(50);
    expect(letters.textContent).toMatch(/^A+$/);
    // Glyphs outside the viewport are culled.
    expect(within(root).getAllByTestId("track-site").length).toBeLessThan(22);

    fireEvent.click(within(root).getByTestId("track-zoom-fit"));
    expect(within(root).getByTestId("track-range").textContent).toBe("1–151");
    expect(within(root).getAllByTestId("track-site")).toHaveLength(22);
  });

  it("renders T as U in the letters row", () => {
    renderGolden({ sequence: "T".repeat(151), selectedKey: "52:Gm" });
    fireEvent.click(screen.getByTestId("track-zoom-in"));
    expect(screen.getByTestId("track-letters").textContent).toMatch(/^U+$/);
  });

  it("shows the not-scored flanks and their explanation", () => {
    renderGolden();
    const flanks = screen.getAllByTestId("track-flank");
    expect(flanks).toHaveLength(2);
    fireEvent.pointerOver(flanks[0]);
    expect(screen.getByTestId("track-tooltip").textContent).toContain("51-nt window");
  });

  it("shows the empty message when the filters removed everything", () => {
    renderGolden({ sites: [] });
    expect(screen.getByTestId("track-empty").textContent).toContain("No sites to display for the current filters");
    expect(screen.queryAllByTestId("track-site")).toHaveLength(0);
    expect(screen.queryAllByTestId("track-lane")).toHaveLength(0);
    expect(screen.getByTestId("track-ruler")).toBeInTheDocument();
  });

  it("only draws the sites it is given (table filter)", () => {
    const subset = goldenRes.results.filter((s) => s.mod_type === "m5C");
    renderGolden({ sites: subset });
    expect(screen.getAllByTestId("track-site")).toHaveLength(subset.length);
    expect(screen.getAllByTestId("track-lane")).toHaveLength(1);
  });

  it("legend mentions attention only when windows are available", () => {
    const { unmount } = renderGolden();
    expect(screen.getByTestId("track-legend").textContent).toContain("regions the model attended to most");
    unmount();
    renderGolden({ attentionByKey: new Map() });
    expect(screen.getByTestId("track-legend").textContent).toContain("not requested");
  });
});

describe("TrackView (many_rows fixture, 400 nt, 894 sites)", () => {
  it("renders every site individually at the fallback width and stays fast", () => {
    const t0 = performance.now();
    render(
      <TrackView
        sequence={manyFixture.request.sequence}
        meta={manyFixture.response.meta}
        sites={manyFixture.response.results}
        attentionByKey={new Map()}
        selectedKey={null}
        onSelect={() => {}}
      />,
    );
    const ms = performance.now() - t0;
    expect(screen.getAllByTestId("track-site")).toHaveLength(894);
    expect(screen.queryAllByTestId("track-bin")).toHaveLength(0);
    expect(ms).toBeLessThan(5000);
  });

  it("brings an externally selected site into view when it is off-screen", () => {
    const { rerender } = render(
      <TrackView
        sequence={null}
        meta={manyFixture.response.meta}
        sites={manyFixture.response.results}
        attentionByKey={new Map()}
        selectedKey={null}
        onSelect={() => {}}
      />,
    );
    // Zoom in three times around the centre: positions near the start are now off-screen.
    for (let i = 0; i < 3; i++) fireEvent.click(screen.getByTestId("track-zoom-in"));
    expect(screen.getByTestId("track-view").querySelector('[data-key="30:Um"], [data-key="30:m5U"], [data-key="30:Psi"]')).toBeNull();
    const target = manyFixture.response.results.find((s) => s.position <= 40)!;
    rerender(
      <TrackView
        sequence={null}
        meta={manyFixture.response.meta}
        sites={manyFixture.response.results}
        attentionByKey={new Map()}
        selectedKey={siteKey(target)}
        onSelect={() => {}}
      />,
    );
    const glyph = screen.getByTestId("track-view").querySelector(`[data-key="${siteKey(target)}"]`);
    expect(glyph).not.toBeNull();
    expect(glyph!.getAttribute("data-selected")).toBe("true");
  });
});

describe("TrackView (synthetic 10,000 nt, 3,000 sites)", () => {
  it("falls back to density bins when there is less than 1 px per nucleotide", () => {
    const n = 10_000;
    const sites = Array.from({ length: 3000 }, (_, i) => ({
      transcript_id: null,
      position: 26 + Math.floor((i * 9949) / 3000),
      mod_type: ["m6A", "Psi", "m5C"][i % 3],
      probability: 0.2 + (i % 8) / 10,
      p_value: 0.01,
      coverage: null,
      source: "sequence" as const,
    }));
    const meta = { ...goldenRes.meta, sequence_length: n, predicted_start: 26, predicted_end: n - 25, n_sites: 3000, attention: null };
    const t0 = performance.now();
    render(<TrackView sequence={null} meta={meta} sites={sites} attentionByKey={new Map()} selectedKey={null} onSelect={() => {}} />);
    const ms = performance.now() - t0;
    expect(screen.queryAllByTestId("track-site")).toHaveLength(0);
    const bins = screen.getAllByTestId("track-bin");
    expect(bins.length).toBeGreaterThan(0);
    expect(bins.length).toBeLessThanOrEqual(3 * 500); // <= width / BIN_PX per lane
    expect(bins.reduce((a, b) => a + Number(b.getAttribute("data-count")), 0)).toBe(3000);
    expect(ms).toBeLessThan(5000);
    // Clicking a bin zooms in until individual sites are drawn.
    fireEvent.click(bins[0]);
    fireEvent.click(screen.getByTestId("track-zoom-in"));
    fireEvent.click(screen.getByTestId("track-zoom-in"));
    expect(screen.getAllByTestId("track-site").length).toBeGreaterThan(0);
  });
});
