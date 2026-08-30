/**
 * User guide for wet-lab users. The anchor ids are part of the UI contract (other
 * components and the E2E tests link to them): quick-start, input, reading-results,
 * flanks, mod-types, multiple-mods, track-view, table-and-csv, limits, citation,
 * privacy, phase2.
 */
import { useEffect, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import type { ModType } from "../api/types";
import {
  CITATION,
  CITATION_TEXT,
  MIT_LICENSE_URL,
  MODEL_COPYRIGHT,
  MULTIRM_REPO_URL,
  PAPER_URL,
} from "../components/layout/about";
import { ExtLink } from "../components/layout/ExtLink";
import { MOD_TYPE_LIST } from "../lib/modTypes";
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
  { id: "privacy", title: "Privacy: no account, no cookies, nothing stored" },
  { id: "phase2", title: "Coming in phase 2: nanopore signal" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

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
  { name: "coverage", meaning: "Empty in the sequence branch. The nanopore branch will report read depth here." },
  { name: "source", meaning: "“sequence” for this branch; the nanopore branch will write “signal”." },
];

export function HelpPage() {
  const { hash } = useLocation();

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
          RModHub predicts RNA modification sites from a nucleotide sequence with the MultiRM model.
          This page explains what the numbers mean, where the model cannot look, and how to turn a
          list of predicted sites into something you can test at the bench.
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
            There is no batch mode or job queue in this phase; for many sequences, call{" "}
            <Code>POST /api/predict/sequence</Code> from a script (see{" "}
            <a href="/docs" className="text-brand-600 underline underline-offset-2">API docs</a>), one
            sequence per call. Request bodies above 1 MB are refused.
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
            never the sequence.
          </li>
          <li>
            The web server keeps a standard access log (client address, request path, status, time)
            for operating the service. The sequence travels in the request body and is not part of it.
          </li>
          <li>The CSV and FASTA downloads are generated for you on the spot and are not kept either.</li>
        </Ul>
      </Section>

      <Section id="phase2">
        <p>
          The sequence branch predicts from sequence alone. The planned{" "}
          <Link to="/signal" className="text-brand-600 underline underline-offset-2">nanopore signal branch</Link>{" "}
          will call modifications from Oxford Nanopore direct-RNA reads with the DirectRM model,
          i.e. from the measured molecules in your sample rather than from what the sequence looks like.
        </p>
        <Ul>
          <li>
            <strong>Input:</strong> a BAM file of basecalled reads with the basecaller’s move table,
            uploaded on the Nanopore signal page. Uploads are large, so a run becomes an asynchronous job.
          </li>
          <li>
            <strong>Jobs:</strong> after the upload you get a job id and a result page{" "}
            (<Code>/result/{"{job_id}"}</Code>) that refreshes itself until the job is done; you can bookmark it.
          </li>
          <li>
            <strong>Output:</strong> the same rows as this branch, with <Code>transcript_id</Code> and{" "}
            <Code>coverage</Code> (read depth) filled in and <Code>source = "signal"</Code>, shown in the
            same table and track view and downloadable as the same CSV.
          </li>
        </Ul>
      </Section>
    </article>
  );
}
