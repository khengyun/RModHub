/**
 * User guide for wet-lab users. The anchor ids are part of the UI contract (other
 * components and the E2E tests link to them): quick-start, input, reading-results,
 * flanks, mod-types, multiple-mods, track-view, table-and-csv, limits, citation,
 * privacy, nanopore-signal (with the signal-* sub-anchors listed in SIGNAL_SUBSECTIONS).
 */
import { useEffect, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import type { ModType } from "../api/types";
import {
  CITATION,
  CITATION_TEXT,
  DIRECTRM_CITATION,
  DIRECTRM_CITATION_TEXT,
  DIRECTRM_COMMIT,
  DIRECTRM_COPYRIGHT,
  DIRECTRM_MODEL_NAME,
  DIRECTRM_PAPER_URL,
  DIRECTRM_REPO_URL,
  MIT_LICENSE_URL,
  MODEL_COPYRIGHT,
  MULTIRM_REPO_URL,
  PAPER_URL,
} from "../components/layout/about";
import { uploadTtlHours, useCapabilities } from "../components/layout/CapabilitiesProvider";
import { ExtLink } from "../components/layout/ExtLink";
import { SUBSET_DOCKER_COMMAND } from "../components/signal/uploadModel";
import { MOD_TYPE_LIST, modTypeInfo, SIGNAL_MOD_TYPES } from "../lib/modTypes";
import { FLANK_NT, MAX_NT, MIN_NT } from "../lib/sequence";

const WINDOW_NT = 2 * FLANK_NT + 1; // 51
/** Negative background sequences shipped with MultiRM (neg_prob.csv): the p-value resolution. */
const N_BACKGROUND = 150;
const P_RESOLUTION = "0.0067"; // 1 / 150, rounded
const MAX_TESTS = (MAX_NT - 2 * FLANK_NT) * 12; // 119,400 for a 10,000-nt input

const SECTIONS = [
  { id: "quick-start", title: "Quick start" },
  { id: "input", title: "What you can paste" },
  { id: "reading-results", title: "Reading the results: probability, p-value, alpha" },
  { id: "flanks", title: "Why the first and last 25 nt are never scored" },
  { id: "mod-types", title: "The 12 modification types" },
  { id: "multiple-mods", title: "Several types at one position" },
  { id: "track-view", title: "The track view" },
  { id: "table-and-csv", title: "The table and the CSV file" },
  { id: "limits", title: "Limits and run time" },
  { id: "citation", title: "How to cite" },
  { id: "privacy", title: "Privacy: no account, no cookies, sequences not stored" },
  { id: "nanopore-signal", title: "Nanopore signal branch (DirectRM)" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

/** Sub-anchors of the nanopore-signal section (linked from the upload and result pages). */
export const SIGNAL_SUBSECTIONS = [
  { id: "signal-files", title: "The four input files and how to produce them" },
  { id: "signal-regions", title: "The regions CSV" },
  { id: "signal-subset", title: "My pod5 is too big: the subset tool" },
  { id: "signal-coverage", title: "Coverage: why 30 reads matter" },
  { id: "signal-results", title: "Reading rate, confidence interval and count" },
  { id: "signal-reads", title: "Read-level drill-down" },
  { id: "signal-jobs", title: "Jobs: stages, limits, cancel, bookmark" },
  { id: "signal-data", title: "What happens to your files" },
  { id: "signal-sample", title: "The sample data (synthetic)" },
  { id: "signal-citation", title: "Citing DirectRM and its components" },
] as const;

type SignalSubId = (typeof SIGNAL_SUBSECTIONS)[number]["id"];

function Sub({ id, children }: { id: SignalSubId; children: ReactNode }) {
  const title = SIGNAL_SUBSECTIONS.find((s) => s.id === id)?.title ?? id;
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-6 space-y-2 pt-2">
      <h3 id={`${id}-title`} className="font-semibold text-slate-800">
        {title}
      </h3>
      {children}
    </section>
  );
}

const FULL_NAME: Record<ModType, string> = {
  Am: "2′-O-methyladenosine",
  Cm: "2′-O-methylcytidine",
  Gm: "2′-O-methylguanosine",
  Um: "2′-O-methyluridine",
  m1A: "N1-methyladenosine",
  m5C: "5-methylcytidine",
  m5U: "5-methyluridine (ribothymidine)",
  m6A: "N6-methyladenosine",
  m6Am: "N6,2′-O-dimethyladenosine",
  m7G: "N7-methylguanosine",
  Psi: "Pseudouridine",
  AtoI: "Adenosine-to-inosine editing",
};

function Section({
  id,
  wide = false,
  children,
}: {
  id: SectionId;
  wide?: boolean;
  children: ReactNode;
}) {
  const title = SECTIONS.find((s) => s.id === id)?.title ?? id;
  return (
    <section id={id} aria-labelledby={`${id}-title`} className="scroll-mt-6">
      <h2 id={`${id}-title`} className="text-lg font-semibold text-brand-800">
        {title}
      </h2>
      <div
        className={`mt-3 space-y-3 text-sm leading-relaxed text-slate-700 ${wide ? "" : "max-w-3xl"}`}
      >
        {children}
      </div>
    </section>
  );
}

function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.85em]">{children}</code>;
}

function Ul({ children }: { children: ReactNode }) {
  return <ul className="list-disc space-y-1 pl-5">{children}</ul>;
}

function Ol({ children }: { children: ReactNode }) {
  return <ol className="list-decimal space-y-1 pl-5">{children}</ol>;
}

function HelpLink({ to, children }: { to: SectionId; children: ReactNode }) {
  return (
    <a href={`#${to}`} className="text-brand-600 underline underline-offset-2 hover:text-brand-800">
      {children}
    </a>
  );
}

/** Sketch of the sliding 51-nt window: only the centre nucleotide of each window is scored. */
function FlankSketch() {
  // 3.6 px per nucleotide: a 25-nt flank is 90 px, a 51-nt window 184 px.
  return (
    <svg
      viewBox="0 0 640 130"
      role="img"
      aria-labelledby="flanks-sketch-title"
      className="w-full max-w-2xl text-slate-700"
    >
      <title id="flanks-sketch-title">
        A 51-nucleotide window slides along the sequence and only its centre nucleotide is
        scored, so the first 25 and the last 25 nucleotides are never at the centre.
      </title>
      <defs>
        <marker id="flank-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
          <path d="M0 0 L8 4 L0 8 z" fill="#475569" />
        </marker>
      </defs>

      {/* first and last window */}
      <rect x="20" y="30" width="184" height="20" rx="3" fill="none" stroke="#1f4e79" strokeWidth="1.5" strokeDasharray="4 3" />
      <rect x="436" y="30" width="184" height="20" rx="3" fill="none" stroke="#1f4e79" strokeWidth="1.5" strokeDasharray="4 3" />
      <text x="112" y="24" textAnchor="middle" fontSize="11" fill="currentColor">first window: positions 1–51</text>
      <text x="528" y="24" textAnchor="middle" fontSize="11" fill="currentColor">last window: N−50 … N</text>
      <line x1="214" y1="40" x2="426" y2="40" stroke="#475569" strokeWidth="1" markerEnd="url(#flank-arrow)" />
      <text x="320" y="36" textAnchor="middle" fontSize="10.5" fill="#475569">slides 1 nt at a time</text>

      {/* the input sequence */}
      <rect x="20" y="60" width="600" height="20" fill="#eef4fb" stroke="#1f4e79" />
      <rect x="20" y="60" width="90" height="20" fill="#e2e8f0" stroke="#1f4e79" />
      <rect x="530" y="60" width="90" height="20" fill="#e2e8f0" stroke="#1f4e79" />
      <text x="65" y="74" textAnchor="middle" fontSize="11" fill="currentColor">1 … 25</text>
      <text x="575" y="74" textAnchor="middle" fontSize="11" fill="currentColor">N−24 … N</text>
      <text x="320" y="74" textAnchor="middle" fontSize="11" fill="currentColor">scored positions: 26 … N−25</text>

      {/* centre nucleotide of the first and last window */}
      <rect x="110" y="60" width="4" height="20" fill="#e6ab02" />
      <rect x="526" y="60" width="4" height="20" fill="#e6ab02" />
      <line x1="112" y1="80" x2="112" y2="104" stroke="#e6ab02" strokeWidth="1.5" />
      <line x1="528" y1="80" x2="528" y2="104" stroke="#e6ab02" strokeWidth="1.5" />
      <text x="65" y="96" textAnchor="middle" fontSize="10.5" fill="#475569">not scored</text>
      <text x="575" y="96" textAnchor="middle" fontSize="10.5" fill="#475569">not scored</text>
      <text x="116" y="118" fontSize="10.5" fill="currentColor">pos. 26 = centre of the first window</text>
      <text x="524" y="118" textAnchor="end" fontSize="10.5" fill="currentColor">pos. N−25 = centre of the last window</text>
    </svg>
  );
}

const CSV_COLUMNS: { name: string; meaning: string }[] = [
  { name: "transcript_id", meaning: "The id from your FASTA header (first word after “>”), otherwise empty." },
  { name: "position", meaning: "1-based position of the scored nucleotide in your sequence, counted after whitespace was removed." },
  { name: "mod_type", meaning: "One of the 12 symbols in the table above, spelled as in the CSV column (“Psi”, “AtoI”)." },
  { name: "probability", meaning: "The model’s output for that type at that position, between 0 and 1." },
  { name: "p_value", meaning: `Empirical p-value against the ${N_BACKGROUND} background sequences (multiples of 1/${N_BACKGROUND}); 0 means “< ${P_RESOLUTION}”.` },
  { name: "coverage", meaning: "Empty in the sequence branch. The nanopore signal branch writes the number of reads that received a score at the base." },
  { name: "source", meaning: "“sequence” for the sequence branch, “signal” for the nanopore signal branch." },
];

const SIGNAL_CSV_COLUMNS: { name: string; meaning: string }[] = [
  { name: "transcript_id … source", meaning: "The same seven columns as above, in the same order (probability = modification rate, p_value empty, coverage filled, source = “signal”)." },
  { name: "strand", meaning: "Strand of the region the site belongs to (from your regions CSV)." },
  { name: "count", meaning: "Reads whose per-read probability for this type is above 0.5 (“modified reads”)." },
  { name: "ci_low, ci_high", meaning: "95 % Wilson score interval of count / coverage." },
  { name: "max_prob, noisyor_prob", meaning: "Alternative per-site summaries DirectRM computes from the same per-read probabilities (maximum, and the noisy-OR combination). Informational." },
];

export function HelpPage() {
  const { hash } = useLocation();
  const { capabilities } = useCapabilities();
  const { limits, retention } = capabilities;
  const uploadTtlH = uploadTtlHours(capabilities);

  // Scroll to the anchor when the page is opened with /help#section from a client-side link.
  useEffect(() => {
    if (!hash) return;
    const el = document.getElementById(hash.slice(1));
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView();
  }, [hash]);

  return (
    <article data-testid="help-page" className="space-y-10">
      <header className="max-w-3xl">
        <h1 className="text-2xl font-semibold text-brand-800">Help: using RModHub and reading its results</h1>
        <p className="mt-2 text-sm leading-relaxed text-slate-600">
          RModHub predicts RNA modification sites from a nucleotide sequence with the MultiRM model
          (sections 1–11) and calls them from the raw signal of Oxford Nanopore direct-RNA reads
          with DirectRM (section 12). This page explains what the numbers mean, where each model
          cannot look, and how to turn a list of sites into something you can test at the bench.
        </p>
      </header>

      <nav aria-label="On this page" className="rounded border border-slate-200 bg-white px-4 py-3 text-sm">
        <p className="font-semibold text-slate-700">On this page</p>
        <ol className="mt-1 grid list-decimal gap-x-8 gap-y-0.5 pl-5 sm:grid-cols-2 lg:grid-cols-3">
          {SECTIONS.map((s) => (
            <li key={s.id}>
              <a href={`#${s.id}`} className="text-brand-600 hover:underline">
                {s.title}
              </a>
            </li>
          ))}
        </ol>
      </nav>

      <Section id="quick-start">
        <Ol>
          <li>
            On the <Link to="/" className="text-brand-600 underline underline-offset-2">Sequence</Link> page,
            paste your sequence into the box, or press <strong>Load sample data</strong> to try the
            built-in example (151 nt from the MultiRM README; it gives 22 sites at alpha = 0.05).
          </li>
          <li>
            Keep the significance level (alpha) at 0.05, or choose 0.01 for a shorter, stricter list.
            See <HelpLink to="reading-results">reading the results</HelpLink> for what it means.
          </li>
          <li>
            Press <strong>Predict modification sites</strong>. Short inputs come back within a second;
            a 10,000-nt input takes about 13 s. You can cancel a running request.
          </li>
          <li>
            Read the summary line, the <HelpLink to="track-view">track view</HelpLink> and the{" "}
            <HelpLink to="table-and-csv">table</HelpLink>. Selecting a site in one highlights it in the other.
          </li>
          <li>
            Press <strong>Download CSV</strong> to keep the rows, or call the same endpoint from a script
            (<a href="/docs" className="text-brand-600 underline underline-offset-2">API docs</a>).
          </li>
        </Ol>
      </Section>

      <Section id="input">
        <Ul>
          <li>
            <strong>Length:</strong> {MIN_NT} to {MAX_NT.toLocaleString("en-US")} nt, counted after
            whitespace is removed. Shorter than {MIN_NT} nt cannot be scored at all (one full window
            is needed); longer inputs are rejected to keep the server responsive for everyone.
          </li>
          <li>
            <strong>Letters:</strong> A, C, G and U or T, in upper or lower case. Spaces, tabs and line
            breaks are ignored, so you can paste directly from a viewer or a FASTA file. U is
            converted to T before scoring, so RNA and DNA spellings of the same sequence give exactly
            the same result (the page shows “U read as T” when that happened).
          </li>
          <li>
            <strong>FASTA:</strong> one record is accepted. A first line starting with <Code>&gt;</Code>{" "}
            is treated as the header; its first word becomes <Code>transcript_id</Code> in the table
            and the CSV. A second <Code>&gt;</Code> line is rejected: one sequence per request.
          </li>
          <li>
            <strong>Anything else</strong> (N, IUPAC codes, gaps, digits) is rejected and the message
            lists the offending characters. Remove or replace them first; note that positions are
            always counted on the sequence exactly as you submitted it (minus whitespace).
          </li>
          <li>
            <strong>Positions</strong> are 1-based: the first nucleotide you pasted is position 1.
          </li>
        </Ul>
      </Section>

      <Section id="reading-results">
        <p>
          Each row of the result is one <em>(position, modification type)</em> pair with two numbers.
        </p>
        <p>
          <strong>Probability</strong> is the model’s raw output for that type at that position, between
          0 and 1. MultiRM has one separate output for each of the 12 types (a sigmoid “head”), so the
          probabilities of different types are independent scores, not shares of 100 %.
        </p>
        <p>
          <strong>p-value</strong> is empirical. MultiRM ships a background of {N_BACKGROUND} negative
          (unmodified) sequences for each type. The p-value of a site is the fraction of those{" "}
          {N_BACKGROUND} background sequences whose probability for that type is <em>higher</em> than
          your site’s probability. Because there are only {N_BACKGROUND} of them, p-values are multiples
          of 1/{N_BACKGROUND} ≈ {P_RESOLUTION}. A p-value of 0 means no background sequence scored higher;
          it is shown as “&lt; {P_RESOLUTION}” because the background cannot resolve anything smaller.
          Among sites tied at “&lt; {P_RESOLUTION}”, rank by probability.
        </p>
        <p>
          <strong>Alpha</strong> is your threshold: a site is listed when its p-value is below alpha.
          The default 0.05 is the conventional 5 % false-positive rate against that background, i.e.
          a listed site scores higher than at least 95 % of the negatives. Only rows with p-value &lt;
          alpha and probability &gt; 0 are returned; alpha = 1 returns every scored pair.
        </p>
        <p>
          <strong>Multiple testing.</strong> p-values are per site and are <em>not</em> corrected for
          multiple testing. A sequence of N nt is scored at N − {2 * FLANK_NT} positions for 12 types,
          so a 10,000-nt input is {MAX_TESTS.toLocaleString("en-US")} separate tests. Under alpha = 0.05
          roughly 5 % of the pairs of a completely unmodified sequence would still pass, which for a
          long input means hundreds or thousands of rows by chance alone. In practice:
        </p>
        <Ul>
          <li>Lower alpha to 0.01 when you want a stringent list; use 0.05 for a sensitive one.</li>
          <li>Sort by p-value, then by probability, and look at the top of the list first.</li>
          <li>
            Prefer sites where the base and the sequence context make sense (see{" "}
            <HelpLink to="mod-types">the modification types</HelpLink>) and where the{" "}
            <HelpLink to="track-view">attention windows</HelpLink> point at a plausible motif.
          </li>
          <li>
            Treat every hit as a <em>candidate</em> for experimental validation with an orthogonal
            method, not as evidence that the base is modified.
          </li>
        </Ul>
      </Section>

      <Section id="flanks">
        <p>
          MultiRM does not look at one nucleotide in isolation. It slides a window of {WINDOW_NT} nt
          along your sequence and, for each window, scores only the nucleotide in the middle, using the{" "}
          {FLANK_NT} nt on either side as context. The first window covers positions 1–{WINDOW_NT} and
          scores position {FLANK_NT + 1}; the last one scores position N − {FLANK_NT}.
        </p>
        <FlankSketch />
        <p>
          So the first {FLANK_NT} and the last {FLANK_NT} nucleotides of whatever you paste are never
          at the centre of a window and never receive a prediction. The summary line reports the scored
          range as “{FLANK_NT + 1}–(N − {FLANK_NT})”. If the region you care about is near the end of
          your transcript, extend the input by at least {FLANK_NT} nt of flanking sequence on that side.
        </p>
      </Section>

      <Section id="mod-types" wide>
        <p className="max-w-3xl">
          The <em>base</em> column is the nucleotide that carries the modification. The model does not
          check it, so a predicted m6A at a position that is not an A, or a pseudouridine at a
          non-U, can be discarded straight away.
        </p>
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="w-full min-w-[40rem] text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
              <tr>
                <th scope="col" className="px-3 py-2">Symbol</th>
                <th scope="col" className="px-3 py-2">In CSV</th>
                <th scope="col" className="px-3 py-2">Full name</th>
                <th scope="col" className="px-3 py-2">Base</th>
                <th scope="col" className="px-3 py-2">What it is</th>
              </tr>
            </thead>
            <tbody>
              {MOD_TYPE_LIST.map((m) => (
                <tr key={m.id} className="border-t border-slate-100 align-top">
                  <td className="whitespace-nowrap px-3 py-2 font-medium">
                    <span className="inline-flex items-center gap-1.5">
                      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                        <rect width="12" height="12" rx="2" fill={m.color} />
                      </svg>
                      {m.label}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">{m.id}</td>
                  <td className="px-3 py-2">{FULL_NAME[m.id]}</td>
                  <td className="px-3 py-2 font-mono">{m.base}</td>
                  <td className="px-3 py-2 text-slate-600">{m.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section id="multiple-mods">
        <p>
          It is normal to see two or more modification types listed at the same position. The 12
          types are 12 independent classifiers looking at the same {WINDOW_NT}-nt window; they are not
          asked to agree with each other, and related modifications (the four 2′-O-methylations,
          m6A and m6Am, m5U and Um) share sequence features.
        </p>
        <p>
          Chemically, one base carries one modification. Read co-called types as{" "}
          <em>alternatives</em>, ranked by p-value and then probability, and check the base first:
          m6A, m1A, m6Am, Am and A-to-I need an A; m5C and Cm a C; m7G and Gm a G; Ψ, m5U and Um a U.
          Then let the biology decide: a cap-proximal A favours m6Am, a tRNA-like T-loop favours m5U,
          a DRACH motif favours m6A.
        </p>
      </Section>

      <Section id="track-view">
        <p>
          The track view draws your sequence left to right with one <strong>lane per modification
          type</strong>, in the colours of the table above. A <strong>glyph</strong> in a lane marks a
          predicted site at that position; the grey ends are the unscored flanks. The track shows the
          rows that are currently visible in the table, so filtering the table also filters the track.
        </p>
        <p>
          Selecting a site (in the track or in the table) shows its <strong>attention windows</strong>{" "}
          as <strong>highlighted boxes</strong> on the sequence: the three 3-nt regions inside the{" "}
          {WINDOW_NT}-nt window that the model weighted most when scoring that site. They are an
          interpretability aid, not a prediction: a window on a recognisable motif (for example a
          DRACH context next to an m6A call) supports the call; windows on featureless sequence do not
          argue against it.
        </p>
      </Section>

      <Section id="table-and-csv" wide>
        <p className="max-w-3xl">
          The table lists one row per (position, type) pair that passed alpha. Use the filters above it
          to restrict the list to particular modification types or a stricter threshold, and click a
          column header to sort. The track view follows the filtered table.
        </p>
        <p className="max-w-3xl">
          <strong>Download CSV</strong> writes the rows with the columns below (the same as the API
          with <Code>?format=csv</Code>). The file opens in Excel, R or Python without any conversion.
        </p>
        <div className="overflow-x-auto rounded border border-slate-200 bg-white">
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
              <tr>
                <th scope="col" className="px-3 py-2">Column</th>
                <th scope="col" className="px-3 py-2">Meaning</th>
              </tr>
            </thead>
            <tbody>
              {CSV_COLUMNS.map((c) => (
                <tr key={c.name} className="border-t border-slate-100 align-top">
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">{c.name}</td>
                  <td className="px-3 py-2 text-slate-600">{c.meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section id="limits">
        <Ul>
          <li>
            Input length {MIN_NT}–{MAX_NT.toLocaleString("en-US")} nt; one sequence per request (a
            multi-record FASTA is rejected). Split longer transcripts into overlapping pieces of at most{" "}
            {MAX_NT.toLocaleString("en-US")} nt; overlap them by at least {2 * FLANK_NT} nt so that no
            position falls in an unscored flank, and keep track of the offsets yourself.
          </li>
          <li>
            Run time grows with length: well under a second for a few hundred nt, about 13 s for a
            10,000-nt input on the server’s single CPU core. Requesting attention windows adds
            10–30 % on long inputs.
          </li>
          <li>
            The sequence branch has no batch mode; for many sequences, call{" "}
            <Code>POST /api/predict/sequence</Code> from a script (see{" "}
            <a href="/docs" className="text-brand-600 underline underline-offset-2">API docs</a>), one
            sequence per call. Request bodies above 1 MB are refused. (Only the nanopore signal
            branch uses a job queue, see <HelpLink to="nanopore-signal">below</HelpLink>.)
          </li>
          <li>
            The model only sees sequence. It has no notion of tissue, condition, isoform or expression
            level; it cannot tell you the modification <em>stoichiometry</em>.
          </li>
        </Ul>
      </Section>

      <Section id="citation">
        <p>
          RModHub serves the published MultiRM model. If you use results from this server, please cite
          the model’s paper:
        </p>
        <blockquote className="border-l-4 border-brand-100 pl-3 text-slate-800">
          {CITATION.authors}. {CITATION.title}. <i>{CITATION.journal}</i> {CITATION.volume},{" "}
          {CITATION.article} ({CITATION.year}). <ExtLink href={PAPER_URL}>doi:{CITATION.doi}</ExtLink>
        </blockquote>
        <p>
          Plain text: <Code>{CITATION_TEXT}</Code>
        </p>
        <p>
          Code and weights: <ExtLink href={MULTIRM_REPO_URL}>{MULTIRM_REPO_URL}</ExtLink> (MIT License,{" "}
          {MODEL_COPYRIGHT}). RModHub itself (server and web interface) is released under the{" "}
          <ExtLink href={MIT_LICENSE_URL}>MIT License</ExtLink>; please also give the web address of
          the server you used and the model version shown in the summary line.
        </p>
      </Section>

      <Section id="privacy">
        <Ul>
          <li>No account, no registration and no e-mail address: the server is open to everyone.</li>
          <li>
            No cookies and no tracking. All scripts, styles, fonts and the API documentation are served
            from this host; nothing is loaded from a third party.
          </li>
          <li>
            Sequences are processed in memory and are <strong>not stored</strong>. The application log
            records only the input length, alpha, the number of sites and the timing of each request,
            never the sequence. Nanopore uploads necessarily live on the server while the job runs;
            see <a href="#signal-data" className="text-brand-600 underline underline-offset-2">what happens to your files</a>.
          </li>
          <li>
            The web server keeps a standard access log (client address, request path, status, time)
            for operating the service. The sequence travels in the request body and is not part of it.
          </li>
          <li>The CSV and FASTA downloads are generated for you on the spot and are not kept either.</li>
        </Ul>
      </Section>

      <Section id="nanopore-signal" wide>
        <p className="max-w-3xl">
          The{" "}
          <Link to="/signal" className="text-brand-600 underline underline-offset-2">Nanopore signal</Link>{" "}
          page calls modifications from what was actually measured in your sample rather than from
          what the sequence looks like. It runs{" "}
          <ExtLink href={DIRECTRM_REPO_URL}>{DIRECTRM_MODEL_NAME}</ExtLink> (Zhang <i>et al.</i>, 2025)
          unmodified on a background worker: for every read that covers one of your regions, the raw
          current is aligned to the basecalled sequence (Remora), k-mer signal features are extracted,
          and a neural network scores each base of each read for m6A, m5C, m1A, m7G, Ψ and ac4C.
          Per-read calls are then summarised per site as a <em>rate</em> with a confidence interval.
          Because the input is large and a run takes minutes, the analysis is a <em>job</em> with its
          own result page. The tab only appears when the server operator has enabled this branch.
        </p>
        <nav aria-label="Nanopore signal subsections" className="max-w-3xl text-xs text-slate-600">
          <ol className="grid list-decimal gap-x-6 gap-y-0.5 pl-5 sm:grid-cols-2">
            {SIGNAL_SUBSECTIONS.map((sub) => (
              <li key={sub.id}>
                <a href={`#${sub.id}`} className="text-brand-600 hover:underline">
                  {sub.title}
                </a>
              </li>
            ))}
          </ol>
        </nav>

        <div className="max-w-3xl space-y-6">
          <Sub id="signal-files">
            <Ol>
              <li>
                <strong>pod5</strong> — the raw signal, as written by MinKNOW (RNA004 or RNA002 kit;
                choose the kit on the upload page). Several pod5 files can be merged with{" "}
                <Code>pod5 merge</Code>; only reads overlapping your regions are needed.
              </li>
              <li>
                <strong>BAM with move table, aligned</strong> — basecall <em>and</em> align in one
                dorado run so that each read keeps its <Code>mv</Code> tag (the signal-to-base
                alignment DirectRM needs), then sort:
                <pre className="mt-1 overflow-x-auto rounded bg-slate-900 px-3 py-2 font-mono text-xs leading-5 text-slate-100">
                  <code>{`dorado basecaller <model> pod5/ --emit-moves --reference ref.fa | samtools sort -o input_sorted.bam
samtools index input_sorted.bam`}</code>
                </pre>
                A BAM basecalled <em>without</em> <Code>--emit-moves</Code>, or aligned separately with
                minimap2 (which drops the tag), fails in the <em>preparing</em> stage (before any signal
                is read) with a clear message. The index (<Code>.bai</Code>) is rebuilt on the server,
                you do not upload it.
              </li>
              <li>
                <strong>Reference FASTA</strong> — exactly the file the reads were aligned to
                (<Code>.fa</Code> or <Code>.fasta</Code>, uncompressed). Transcript names must match
                the BAM and the regions CSV character for character.
              </li>
              <li>
                <strong>Regions CSV</strong> — which transcripts, coordinates and strands to score; see
                the next section.
              </li>
            </Ol>
          </Sub>

          <Sub id="signal-regions">
            <p>
              DirectRM scores regions, not whole genomes: sampling up to 150 reads per region keeps a
              job within minutes and makes coverage explicit. The file is a comma-separated table with
              a header line and these columns:
            </p>
            <pre className="overflow-x-auto rounded bg-slate-900 px-3 py-2 font-mono text-xs leading-5 text-slate-100">
              <code>{`seqnames,start,end,width,strand
tx_A,1,1200,1200,+
tx_B,101,900,800,+`}</code>
            </pre>
            <Ul>
              <li>
                <strong>Coordinates are 1-based and inclusive</strong> (like a GFF or a genome
                browser, not like BED): <Code>start = 1</Code> is the first base, and{" "}
                <Code>width = end − start + 1</Code>.
              </li>
              <li>
                <strong>strand</strong> is <Code>+</Code> or <Code>−</Code> and must match the
                alignment orientation of the reads you want scored (direct-RNA reads normally align to
                the transcript's + strand).
              </li>
              <li>
                <strong>The first base of a region is never scored</strong>: DirectRM's k-mer window
                starts at the second base. Start regions one base early if the very first position
                matters.
              </li>
              <li>
                Up to {limits.max_regions.toLocaleString("en-US")} data rows; regions with 30 reads or
                fewer are skipped (see coverage below), regions with 150 reads or more are randomly
                subsampled to 150.
              </li>
            </Ul>
          </Sub>

          <Sub id="signal-subset">
            <p>
              A whole-run pod5 is often tens of gigabytes, far above the {limits.max_pod5_gb} GB upload
              limit, while the reads in a few regions are a few megabytes. The subset tool in the
              RModHub repository (<Code>tools/</Code>) keeps only the reads that overlap your regions and
              writes a small pod5 plus the matching BAM. It runs locally in Docker (build the image from
              the repository; nothing is downloaded from a third party):
            </p>
            <pre className="overflow-x-auto rounded bg-slate-900 px-3 py-2 font-mono text-xs leading-5 text-slate-100">
              <code>{SUBSET_DOCKER_COMMAND}</code>
            </pre>
            <p>
              <Code>-i</Code> the big pod5 (or a directory), <Code>-b</Code> the sorted, indexed BAM with
              move tables, <Code>-r</Code> the regions CSV you will upload, <Code>-o</Code> /{" "}
              <Code>--bam-out</Code> the files to upload. <strong>Size estimate:</strong> the subset is
              roughly <em>pod5 size × (reads in your regions ÷ reads in the run)</em>; the upload page has
              a small calculator for this. <Code>samtools view -c in.bam</Code> counts all reads and{" "}
              <Code>samtools view -c -L regions.bed in.bam</Code> the reads in your regions.
            </p>
          </Sub>

          <Sub id="signal-coverage">
            <Ul>
              <li>
                <strong>More than 30 reads per region are required.</strong> DirectRM's sampling step
                drops any region with 30 reads or fewer before extracting features; the summary line
                of the result page reports how many regions were skipped for this reason.
              </li>
              <li>
                <strong>Coverage</strong> in the table is the number of reads that received a score at
                that base for that modification type — fewer than the raw read depth, because reads
                that are soft-clipped, poorly aligned or failed signal refinement at that base do not
                count.
              </li>
              <li>
                <strong>Sites with coverage below 30 are flagged as unreliable</strong> (yellow notice
                and the Coverage column): the rate rests on a handful of per-read calls and its
                confidence interval is wide. Sort by coverage and inspect the read-level calls before
                drawing conclusions.
              </li>
            </Ul>
          </Sub>

          <Sub id="signal-results">
            <p>
              Each row of a signal result is one <em>(transcript, position, strand, modification
              type)</em> with:
            </p>
            <Ul>
              <li>
                <strong>Rate</strong> (shown in the Probability column and as glyph height in the
                track view) = <em>count ÷ coverage</em>, the fraction of scored reads called modified at
                that base. It is an estimate of the modification stoichiometry in your sample, not a
                confidence that the site is modified at all.
              </li>
              <li>
                <strong>95 % CI</strong> = Wilson score interval of that fraction. With 40 reads a rate
                of 0.50 comes with an interval of about 0.35–0.65; with 12 reads the same rate spans
                0.25–0.75. Two sites whose intervals overlap should not be called "different".
              </li>
              <li>
                <strong>Count</strong> (Modified reads) = reads whose per-read probability for this type
                is above 0.5.
              </li>
              <li>
                <strong>p-value</strong> is empty: DirectRM has no background model. Use coverage and the
                interval width instead.
              </li>
            </Ul>
            <p>
              Several types at one position are possible (each type is a separate classifier reading
              the same signal); check the base first, as for the sequence branch, and prefer the type
              whose interval is narrow and far from zero.
            </p>
            <div className="overflow-x-auto rounded border border-slate-200 bg-white">
              <table className="w-full min-w-[36rem] text-left text-sm">
                <caption className="sr-only">Modification types called by DirectRM</caption>
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                  <tr>
                    <th scope="col" className="px-3 py-2">Symbol</th>
                    <th scope="col" className="px-3 py-2">In CSV</th>
                    <th scope="col" className="px-3 py-2">Base</th>
                    <th scope="col" className="px-3 py-2">What it is</th>
                  </tr>
                </thead>
                <tbody data-testid="signal-mod-types">
                  {SIGNAL_MOD_TYPES.map((id) => {
                    const m = modTypeInfo(id);
                    return (
                      <tr key={id} className="border-t border-slate-100 align-top">
                        <td className="whitespace-nowrap px-3 py-2 font-medium">
                          <span className="inline-flex items-center gap-1.5">
                            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                              <rect width="12" height="12" rx="2" fill={m.color} />
                            </svg>
                            {m.label}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">{m.id}</td>
                        <td className="px-3 py-2 font-mono">{m.base}</td>
                        <td className="px-3 py-2 text-slate-600">{m.description}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p>
              <strong>CSV.</strong> <em>Download CSV</em> on a result page (or{" "}
              <Code>GET /api/jobs/&#123;job_id&#125;/download.csv?level=site</Code>) writes the shared seven
              columns followed by the signal-specific ones:
            </p>
            <div className="overflow-x-auto rounded border border-slate-200 bg-white">
              <table className="w-full min-w-[36rem] text-left text-sm">
                <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
                  <tr>
                    <th scope="col" className="px-3 py-2">Column</th>
                    <th scope="col" className="px-3 py-2">Meaning</th>
                  </tr>
                </thead>
                <tbody>
                  {SIGNAL_CSV_COLUMNS.map((c) => (
                    <tr key={c.name} className="border-t border-slate-100 align-top">
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-xs">{c.name}</td>
                      <td className="px-3 py-2 text-slate-600">{c.meaning}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Sub>

          <Sub id="signal-reads">
            <p>
              Select a site in the table or the track view and a <strong>read-level panel</strong>{" "}
              opens below the table: one row per read that was scored at that base, with the read id,
              strand, the model's per-read probability and whether it counted as modified (&gt; 0.5).
              This is the evidence behind the rate: a site whose reads sit near 0.5 is weaker than one
              whose reads are split between 0.05 and 0.95. The panel is paged; download the loaded page
              as CSV, or <em>all</em> read-level calls of the job (<Code>download.csv?level=read</Code>;
              columns read_id, transcript_id, position, strand, mod_type, probability, source).
            </p>
          </Sub>

          <Sub id="signal-jobs">
            <Ul>
              <li>
                <strong>Stages</strong>, in order: uploading → preparing (BAM index, checks) → sampling
                reads → extracting features (the longest) → de novo screen → inference → aggregating.
                The result page shows the current stage with a one-line explanation, a progress bar
                for the stage and an estimated time when one is available.
              </li>
              <li>
                <strong>Limits:</strong> {limits.max_running_per_ip} running and {limits.max_queued_per_ip}{" "}
                queued jobs per network address; a job is stopped after {limits.job_timeout_h} h. Jobs
                run one at a time on a single CPU worker, so a queue position can mean a wait.
              </li>
              <li>
                <strong>Cancel</strong> with the button on the result page; the worker stops between
                stages (or immediately, when running) and the job's files are removed.
              </li>
              <li>
                <strong>Bookmark the result page.</strong> The URL <Code>/result/&#123;job_id&#125;</Code>{" "}
                is the <em>only</em> key to a job: there is no account and no e-mail. Anyone with the
                link can view the results, so share it deliberately. The page refreshes itself while
                the job runs (every 2–10 s); it shows <em>Expired</em> once the results have been
                deleted and "unknown" for an id the server does not know.
              </li>
              <li>
                <strong>Interrupted uploads</strong> retry by themselves for about two minutes after a
                network hiccup (and wait while your browser is offline); after that, press{" "}
                <em>Resume upload</em>. After a reload or on another day, pick the same four files again
                and the page offers to resume the earlier upload (it remembers file name, size and
                upload URL in your browser's local storage for at most {uploadTtlH} h, as long as the
                server keeps the unfinished upload).
              </li>
            </Ul>
          </Sub>

          <Sub id="signal-data">
            <Ul>
              <li>Uploaded files are used only for your job and are never shared or reused.</li>
              <li>
                The pod5 and the BAM are deleted <strong>{retention.inputs_deleted}</strong>; the
                reference and the regions file stay with the job until it is deleted.
              </li>
              <li>
                Results (a per-job database with the site- and read-level calls) are kept for{" "}
                <strong>{retention.results_days} days</strong> after the job finished, then deleted
                together with everything else. Unfinished uploads expire after {uploadTtlH} h.
              </li>
              <li>
                The server keeps a standard access log (client address, path, status, time) and hashes
                the client address to enforce the per-address limits; raw addresses are not stored with
                jobs.
              </li>
            </Ul>
          </Sub>

          <Sub id="signal-sample">
            <p>
              <em>Load sample data</em> on the Nanopore signal page queues a job on a small{" "}
              <strong>synthetic</strong> data set that ships with the server (about 1.4 MB): RNA004-like
              reads generated in silico for three transcripts — <Code>tx_A</Code> (40 reads),{" "}
              <Code>tx_B</Code> (36 reads) and <Code>tx_C</Code> (12 reads, i.e. below the 30-read
              threshold, so that region is skipped and the coverage filter is visible). It exercises
              every stage end to end in well under a minute of worker time (plus any wait in the
              queue). Because the reads are synthetic, the called
              sites carry no biological meaning; use the sample to learn the interface, not to draw
              conclusions. The four files can also be downloaded from the upload page.
            </p>
          </Sub>

          <Sub id="signal-citation">
            <p>If you use signal-branch results, please cite DirectRM:</p>
            <blockquote className="border-l-4 border-brand-100 pl-3 text-slate-800">
              {DIRECTRM_CITATION.authors} {DIRECTRM_MODEL_NAME}. <i>{DIRECTRM_CITATION.journal}</i>{" "}
              {DIRECTRM_CITATION.volume}, {DIRECTRM_CITATION.article} ({DIRECTRM_CITATION.year}).{" "}
              <ExtLink href={DIRECTRM_PAPER_URL}>doi:{DIRECTRM_CITATION.doi}</ExtLink>
            </blockquote>
            <p>
              Plain text: <Code>{DIRECTRM_CITATION_TEXT}</Code>
            </p>
            <Ul>
              <li>
                Code and weights: <ExtLink href={DIRECTRM_REPO_URL}>{DIRECTRM_REPO_URL}</ExtLink> (MIT
                License, {DIRECTRM_COPYRIGHT}), commit <Code>{DIRECTRM_COMMIT}</Code>, run unmodified.
                Weights were re-saved for CPU without changing any value.
              </li>
              <li>
                Signal-to-base alignment and k-mer level tables: Remora (Oxford Nanopore Technologies
                Public License 1.0, <em>research use only</em>) and ONT <Code>kmer_models</Code>{" "}
                (MPL-2.0). By using this branch you accept those terms for the corresponding components.
              </li>
              <li>
                Please also give the web address of the server you used and the model version shown in
                the result summary.
              </li>
            </Ul>
          </Sub>
        </div>
      </Section>
    </article>
  );
}
