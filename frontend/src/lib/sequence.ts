/**
 * Client-side mirror of the backend's input normalisation (app/api/normalize.py), used
 * only to (a) show the user what will be scored and (b) give the track view the
 * nucleotide letters. The backend remains the source of truth; if the lengths disagree
 * (meta.sequence_length) the UI falls back to positions only.
 */

export const MIN_NT = 51;
export const MAX_NT = 10_000;
/** MultiRM scores the centre of a 51-nt window: no predictions for the first/last 25 nt. */
export const FLANK_NT = 25;

export interface ClientNormalized {
  sequence: string; // upper-case ACGT (U -> T), whitespace removed
  transcriptId: string | null;
  hadU: boolean;
  /** Characters outside ACGUT, in first-seen order (max 5), for a friendly local error. */
  invalidChars: string[];
  /** More than one FASTA record was pasted (a `>` line after the first). */
  multiRecord: boolean;
}

export function normalizeSequenceClient(raw: string): ClientNormalized {
  let text = raw.replace(/^[﻿ \t\r\n]+/, "");
  let transcriptId: string | null = null;
  if (text.startsWith(">")) {
    const nl = text.indexOf("\n");
    const header = nl === -1 ? text : text.slice(0, nl);
    text = nl === -1 ? "" : text.slice(nl + 1);
    const tokens = header.slice(1).trim().split(/\s+/).filter(Boolean);
    transcriptId = tokens[0] ?? null;
  }
  // Mirrors the backend: a further `>` line means several records were pasted.
  const multiRecord = text.split("\n").some((line) => line.trimStart().startsWith(">"));
  const upper = text.replace(/\s+/g, "").toUpperCase();
  const invalid: string[] = [];
  for (const ch of upper) {
    if (!"ACGUT".includes(ch) && !invalid.includes(ch)) {
      invalid.push(ch);
      if (invalid.length === 5) break;
    }
  }
  const hadU = upper.includes("U");
  return {
    sequence: upper.replace(/U/g, "T"),
    transcriptId,
    hadU,
    invalidChars: invalid,
    multiRecord,
  };
}

/** Build a single-record FASTA string (60-column lines) for the sample download. */
export function toFasta(id: string, sequence: string): string {
  const lines = sequence.match(/.{1,60}/g) ?? [];
  return `>${id}\n${lines.join("\n")}\n`;
}

export function formatNt(n: number): string {
  return `${n.toLocaleString("en-US")} nt`;
}
