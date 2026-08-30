import { describe, expect, it } from "vitest";
import { MOD_TYPES, siteKey, type ModSite, type PredictionMeta } from "../../api/types";
import golden from "../../api/fixtures/golden_attention.json";
import goldenFasta from "../../api/fixtures/golden_fasta.json";
import manyRows from "../../api/fixtures/many_rows.json";
import {
  allModTypes,
  CSV_HEADER,
  csvFilename,
  DEFAULT_SORT,
  defaultFilterInputs,
  filterSites,
  matchesText,
  modTypeCounts,
  modTypeRank,
  pageOf,
  paginate,
  parseNumber,
  sortSites,
  toCsv,
  toFilters,
  visibleColumns,
  type Filters,
} from "./resultsModel";

const rows = golden.results as ModSite[];
const meta = golden.meta as unknown as PredictionMeta;
const many = manyRows.response.results as ModSite[];
const fastaRows = goldenFasta.results as ModSite[];

const base: Filters = toFilters(defaultFilterInputs(meta, rows));

describe("fixtures", () => {
  it("golden_attention has 22 rows, many_rows has 894", () => {
    expect(rows).toHaveLength(22);
    expect(many).toHaveLength(894);
  });
});

describe("filterSites", () => {
  it("default filters keep every row", () => {
    expect(filterSites(rows, base)).toHaveLength(22);
    expect(base.pMax).toBe(0.05);
    expect(base.posMin).toBe(26);
    expect(base.posMax).toBe(126);
  });

  it("modification type: m5C only", () => {
    const out = filterSites(rows, { ...base, modTypes: new Set(["m5C"]) });
    expect(out).toHaveLength(6);
    expect(out.every((r) => r.mod_type === "m5C")).toBe(true);
  });

  it("empty type set keeps nothing", () => {
    expect(filterSites(rows, { ...base, modTypes: new Set() })).toHaveLength(0);
  });

  it("p-value <= 0.03", () => {
    const out = filterSites(rows, { ...base, pMax: 0.03 });
    expect(out).toHaveLength(11);
    expect(out.every((r) => r.p_value !== null && r.p_value <= 0.03)).toBe(true);
  });

  it("null p-values pass the p-value filter", () => {
    const withNull: ModSite[] = [{ ...rows[0], p_value: null }, rows[1]];
    expect(filterSites(withNull, { ...base, pMax: 0 })).toEqual([withNull[0]]);
  });

  it("minimum probability", () => {
    const out = filterSites(rows, { ...base, probMin: 0.5 });
    expect(out.length).toBeGreaterThan(0);
    expect(out.every((r) => r.probability >= 0.5)).toBe(true);
    expect(filterSites(rows, { ...base, probMin: 0.99 })).toHaveLength(0);
  });

  it("position 79 yields two rows (Cm and m5C), never merged", () => {
    const out = filterSites(rows, { ...base, posMin: 79, posMax: 79 });
    expect(out.map(siteKey)).toEqual(["79:Cm", "79:m5C"]);
  });

  it("position range bounds are inclusive", () => {
    const out = filterSites(rows, { ...base, posMin: 52, posMax: 63 });
    expect(out.map(siteKey)).toEqual(["52:Gm", "63:m5C"]);
  });

  it("quick text filter matches a position exactly or a type by substring", () => {
    expect(filterSites(rows, { ...base, text: "79" }).map(siteKey)).toEqual(["79:Cm", "79:m5C"]);
    expect(filterSites(rows, { ...base, text: "m5c" })).toHaveLength(6);
    expect(filterSites(rows, { ...base, text: "psi" }).map(siteKey)).toEqual(["123:Psi"]);
    expect(filterSites(rows, { ...base, text: "123 m5U" }).map(siteKey)).toEqual(["123:m5U"]);
    expect(filterSites(rows, { ...base, text: "   " })).toHaveLength(22);
    expect(matchesText(rows[0], "nonsense")).toBe(false);
  });
});

describe("sortSites", () => {
  it("default sort = position asc, ties in MOD_TYPES order (matches the API order)", () => {
    const shuffled = [...rows].reverse();
    expect(sortSites(shuffled, DEFAULT_SORT).map(siteKey)).toEqual(rows.map(siteKey));
    // Position 123 carries Um, m5U and Psi in canonical order.
    expect(sortSites(shuffled, DEFAULT_SORT).filter((r) => r.position === 123).map((r) => r.mod_type)).toEqual([
      "Um",
      "m5U",
      "Psi",
    ]);
  });

  it("sort by p-value asc: first row has the minimum p", () => {
    const out = sortSites(rows, { key: "p_value", dir: "asc" });
    const min = Math.min(...rows.map((r) => r.p_value as number));
    expect(out[0].p_value).toBe(min);
    expect(siteKey(out[0])).toBe("107:Gm");
    for (let i = 1; i < out.length; i++) {
      expect(out[i].p_value as number).toBeGreaterThanOrEqual(out[i - 1].p_value as number);
    }
  });

  it("sort by probability desc: first row has the maximum probability", () => {
    const out = sortSites(rows, { key: "probability", dir: "desc" });
    expect(out[0].probability).toBe(Math.max(...rows.map((r) => r.probability)));
  });

  it("mod_type sorts by MOD_TYPES index, not alphabetically", () => {
    const out = sortSites(rows, { key: "mod_type", dir: "asc" }).map((r) => r.mod_type);
    const ranks = out.map(modTypeRank);
    for (let i = 1; i < ranks.length; i++) expect(ranks[i]).toBeGreaterThanOrEqual(ranks[i - 1]);
    // Alphabetically "Psi" < "Um" < "m1A"; canonically m1A (4) < m5C (5) < Psi (10).
    expect(out.indexOf("m1A")).toBeLessThan(out.indexOf("m5C"));
    expect(out.lastIndexOf("Um")).toBeLessThan(out.indexOf("Psi"));
    expect(out[out.length - 1]).toBe("Psi");
    expect(modTypeRank("Am")).toBe(0);
    expect(modTypeRank("unknown")).toBe(MOD_TYPES.length);
  });

  it("nulls sort last in both directions and the input is not mutated", () => {
    const withNull: ModSite[] = [{ ...rows[0], p_value: null }, rows[1], rows[2]];
    const copy = [...withNull];
    expect(sortSites(withNull, { key: "p_value", dir: "asc" }).at(-1)?.p_value).toBeNull();
    expect(sortSites(withNull, { key: "p_value", dir: "desc" }).at(-1)?.p_value).toBeNull();
    expect(withNull).toEqual(copy);
  });
});

describe("paginate", () => {
  it("894 rows -> 18 pages of 50, last page has 44 rows", () => {
    const first = paginate(many, 1, 50);
    expect(first.pageCount).toBe(18);
    expect(first.items).toHaveLength(50);
    expect(first.start).toBe(0);
    expect(first.total).toBe(894);
    const last = paginate(many, 18, 50);
    expect(last.items).toHaveLength(44);
    expect(last.start).toBe(850);
    expect(paginate(many, 1, 250).pageCount).toBe(4);
    expect(paginate(many, 1, 25).pageCount).toBe(36);
  });

  it("clamps out-of-range pages and copes with an empty list", () => {
    expect(paginate(many, 99, 50).page).toBe(18);
    expect(paginate(many, 0, 50).page).toBe(1);
    const empty = paginate([], 3, 50);
    expect(empty.page).toBe(1);
    expect(empty.pageCount).toBe(1);
    expect(empty.items).toEqual([]);
  });

  it("pageOf maps a row index to its page", () => {
    expect(pageOf(0, 50)).toBe(1);
    expect(pageOf(49, 50)).toBe(1);
    expect(pageOf(50, 50)).toBe(2);
    expect(pageOf(893, 50)).toBe(18);
  });
});

describe("toCsv", () => {
  it("golden rows -> header + 22 lines, exact backend header, empty cells for null", () => {
    const csv = toCsv(rows);
    expect(csv.endsWith("\n")).toBe(true);
    const lines = csv.trimEnd().split("\n");
    expect(lines).toHaveLength(23);
    expect(lines[0]).toBe("transcript_id,position,mod_type,probability,p_value,coverage,source");
    expect(lines[0]).toBe(CSV_HEADER.join(","));
    expect(lines[1]).toBe(`,52,Gm,${rows[0].probability},${rows[0].p_value},,sequence`);
  });

  it("keeps transcript ids and quotes cells that need it", () => {
    expect(toCsv(fastaRows).split("\n")[1].startsWith("tx1,52,Gm,")).toBe(true);
    const odd: ModSite = { ...rows[0], transcript_id: 'a,b "c"' };
    expect(toCsv([odd]).split("\n")[1].startsWith('"a,b ""c""",52,')).toBe(true);
  });

  it("csvFilename follows rmodhub_sites_{id|sequence}_{len}nt.csv", () => {
    expect(csvFilename(meta)).toBe("rmodhub_sites_sequence_151nt.csv");
    expect(csvFilename({ transcript_id: "tx1", sequence_length: 151 })).toBe("rmodhub_sites_tx1_151nt.csv");
    expect(csvFilename({ transcript_id: "a/b c", sequence_length: 60 }, "filtered")).toBe(
      "rmodhub_sites_a_b_c_60nt_filtered.csv",
    );
  });
});

describe("columns and helpers", () => {
  it("transcript / coverage columns only appear when some row has a value", () => {
    expect(visibleColumns(rows).map((c) => c.id)).toEqual(["index", "position", "mod_type", "probability", "p_value"]);
    expect(visibleColumns(fastaRows).map((c) => c.id)).toContain("transcript_id");
    expect(visibleColumns(fastaRows).map((c) => c.id)).not.toContain("coverage");
    const signal: ModSite[] = [{ ...rows[0], coverage: 12, source: "signal" }];
    expect(visibleColumns(signal).map((c) => c.id)).toContain("coverage");
  });

  it("modTypeCounts / allModTypes", () => {
    const counts = modTypeCounts(rows);
    expect(counts.get("m5C")).toBe(6);
    expect(counts.get("Am")).toBeUndefined();
    expect(allModTypes(rows)).toEqual([...MOD_TYPES]);
    expect(allModTypes([{ ...rows[0], mod_type: "X" }])).toEqual([...MOD_TYPES, "X"]);
  });

  it("toFilters parses the text inputs; blanks mean no constraint", () => {
    expect(parseNumber("")).toBeNull();
    expect(parseNumber("abc")).toBeNull();
    expect(parseNumber(" 0.03 ")).toBe(0.03);
    const f = toFilters({ ...defaultFilterInputs(meta, rows), pMax: "", posMin: "x", posMax: "100" });
    expect(f.pMax).toBeNull();
    expect(f.posMin).toBeNull();
    expect(f.posMax).toBe(100);
    expect(filterSites(rows, f).every((r) => r.position <= 100)).toBe(true);
  });
});
