/**
 * The 12 modification types MultiRM predicts, in the backend's canonical order, with a
 * distinct colour (shared by the table badges and the track view glyphs) and a one-line
 * description (used by the Help page and tooltips).
 */
import { MOD_TYPES, type ModType } from "../api/types";

export interface ModTypeInfo {
  id: ModType;
  label: string;
  /** Which nucleotide carries the modification (used to sanity-check positions). */
  base: "A" | "C" | "G" | "U";
  color: string;
  description: string;
}

export const MOD_TYPE_INFO: Record<ModType, ModTypeInfo> = {
  Am:   { id: "Am",   label: "Am",   base: "A", color: "#1f77b4", description: "2'-O-methyladenosine: methyl group on the ribose 2'-OH of an A; affects RNA structure and immune recognition." },
  Cm:   { id: "Cm",   label: "Cm",   base: "C", color: "#ff7f0e", description: "2'-O-methylcytidine: ribose 2'-O-methylation of a C, common in rRNA, tRNA and mRNA caps." },
  Gm:   { id: "Gm",   label: "Gm",   base: "G", color: "#2ca02c", description: "2'-O-methylguanosine: ribose 2'-O-methylation of a G." },
  Um:   { id: "Um",   label: "Um",   base: "U", color: "#d62728", description: "2'-O-methyluridine: ribose 2'-O-methylation of a U." },
  m1A:  { id: "m1A",  label: "m1A",  base: "A", color: "#9467bd", description: "N1-methyladenosine: methyl on the Watson-Crick face of A; blocks base pairing, enriched in tRNA/rRNA and some mRNA 5' regions." },
  m5C:  { id: "m5C",  label: "m5C",  base: "C", color: "#8c564b", description: "5-methylcytosine: methyl on C5 of cytosine; involved in mRNA export and stability (NSUN2/ALYREF axis)." },
  m5U:  { id: "m5U",  label: "m5U",  base: "U", color: "#e377c2", description: "5-methyluridine (ribothymidine): methyl on C5 of uridine, classic tRNA T-loop modification." },
  m6A:  { id: "m6A",  label: "m6A",  base: "A", color: "#17becf", description: "N6-methyladenosine: the most abundant internal mRNA modification (METTL3/14 writers, YTH readers); regulates splicing, decay and translation." },
  m6Am: { id: "m6Am", label: "m6Am", base: "A", color: "#bcbd22", description: "N6,2'-O-dimethyladenosine: m6A plus ribose methylation, typically at the first transcribed nucleotide after the cap." },
  m7G:  { id: "m7G",  label: "m7G",  base: "G", color: "#7f7f7f", description: "N7-methylguanosine: the cap modification, also found internally in mRNA, tRNA and rRNA." },
  Psi:  { id: "Psi",  label: "Ψ (Psi)", base: "U", color: "#e6ab02", description: "Pseudouridine: C-C glycosidic isomer of uridine; stabilises RNA structure and can alter stop-codon decoding." },
  AtoI: { id: "AtoI", label: "A-to-I", base: "A", color: "#1b9e77", description: "Adenosine-to-inosine editing by ADAR enzymes; inosine is read as G, so it can recode codons and alter splicing." },
};

export const MOD_TYPE_LIST: ModTypeInfo[] = MOD_TYPES.map((id) => MOD_TYPE_INFO[id]);

export function modTypeInfo(id: string): ModTypeInfo {
  return (
    MOD_TYPE_INFO[id as ModType] ?? {
      id: id as ModType,
      label: id,
      base: "A",
      color: "#334155",
      description: "Unknown modification type.",
    }
  );
}
