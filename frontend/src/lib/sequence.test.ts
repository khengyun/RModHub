import { describe, expect, it } from "vitest";
import golden from "../api/fixtures/golden_attention.json";
import sample from "../api/fixtures/sample.json";
import { normalizeSequenceClient, toFasta } from "./sequence";

describe("normalizeSequenceClient (mirrors app/api/normalize.py)", () => {
  it("keeps a clean DNA sequence and reports its length", () => {
    const n = normalizeSequenceClient(sample.sequence);
    expect(n.sequence).toBe(sample.sequence);
    expect(n.sequence.length).toBe(golden.meta.sequence_length);
    expect(n.transcriptId).toBeNull();
    expect(n.hadU).toBe(false);
    expect(n.invalidChars).toEqual([]);
  });

  it("maps U to T, upper-cases and strips whitespace / CRLF", () => {
    const rna = sample.sequence.toLowerCase().replace(/t/g, "u");
    const wrapped = (rna.match(/.{1,60}/g) ?? []).join("\r\n") + "\n";
    const n = normalizeSequenceClient(wrapped);
    expect(n.sequence).toBe(sample.sequence);
    expect(n.hadU).toBe(true);
  });

  it("takes the transcript id from a FASTA header", () => {
    const n = normalizeSequenceClient(">tx1 some description\n" + sample.sequence);
    expect(n.transcriptId).toBe("tx1");
    expect(n.sequence).toBe(sample.sequence);
  });

  it("flags a second FASTA record instead of treating '>' as an invalid character", () => {
    const n = normalizeSequenceClient(`>a\n${sample.sequence}\n>b\n${sample.sequence}`);
    expect(n.multiRecord).toBe(true);
    expect(normalizeSequenceClient(">only\n" + sample.sequence).multiRecord).toBe(false);
  });

  it("lists up to five distinct invalid characters in first-seen order", () => {
    const n = normalizeSequenceClient("ACGTN-X.ACGTN1234567");
    expect(n.invalidChars).toEqual(["N", "-", "X", ".", "1"]);
  });

  it("returns an empty sequence for empty input", () => {
    expect(normalizeSequenceClient("  \n ").sequence).toBe("");
  });
});

describe("toFasta", () => {
  it("wraps at 60 columns with a header", () => {
    const fasta = toFasta("multirm_readme_151nt", sample.sequence);
    const lines = fasta.trimEnd().split("\n");
    expect(lines[0]).toBe(">multirm_readme_151nt");
    expect(lines.slice(1).every((l) => l.length <= 60)).toBe(true);
    expect(lines.slice(1).join("")).toBe(sample.sequence);
  });
});
