/**
 * Placeholder page for the nanopore-signal branch (phase 2). It only describes what is
 * coming: no form, no upload, and nothing here talks to the server.
 */
import { Link } from "react-router-dom";

function Code({ children }: { children: string }) {
  return <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.85em]">{children}</code>;
}

export function SignalPage() {
  return (
    <article data-testid="signal-page" className="max-w-3xl space-y-8 text-sm leading-relaxed text-slate-700">
      <header>
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Phase 2 · in development</p>
        <h1 className="mt-1 text-2xl font-semibold text-brand-800">Nanopore signal input — coming in phase 2</h1>
        <p className="mt-2 text-slate-600">
          The <Link to="/" className="text-brand-600 underline underline-offset-2">sequence branch</Link>{" "}
          predicts modifications from the nucleotide sequence alone. This branch will call them from
          the electrical signal of Oxford Nanopore direct-RNA reads, which reflects the chemical state
          of the molecules in your sample, using the DirectRM model.
        </p>
      </header>

      <section aria-labelledby="signal-input-title" className="space-y-2">
        <h2 id="signal-input-title" className="text-lg font-semibold text-brand-800">Planned input</h2>
        <ul className="list-disc space-y-1 pl-5">
          <li>
            A <strong>BAM file</strong> of basecalled direct-RNA reads that carries the basecaller’s{" "}
            <strong>move table</strong> (the per-read alignment between signal samples and called
            bases), aligned to a transcript reference so that sites can be reported per transcript.
          </li>
          <li>
            A built-in sample (small BAM + move table) for a one-click test run, like{" "}
            <em>Load sample data</em> on the sequence page.
          </li>
          <li>
            No account and no e-mail: the upload form will be open like the rest of the server.
            Exact file requirements (basecaller versions, tags) will be documented here at launch.
          </li>
        </ul>
      </section>

      <section aria-labelledby="signal-jobs-title" className="space-y-2">
        <h2 id="signal-jobs-title" className="text-lg font-semibold text-brand-800">How a run will work</h2>
        <ol className="list-decimal space-y-1 pl-5">
          <li>
            You upload the files. Because they are large and the analysis takes minutes, the server
            answers immediately with a <strong>job id</strong> instead of a result.
          </li>
          <li>
            You are taken to <Code>{"/result/{job_id}"}</Code>, a page that refreshes itself until
            the job has finished. Bookmark it: the job id is the only key to the result, so keep it
            private; results are deleted after a retention period.
          </li>
          <li>
            Jobs run one after another in a background worker, so the sequence branch stays fast
            while signal jobs are queued.
          </li>
        </ol>
      </section>

      <section aria-labelledby="signal-results-title" className="space-y-2">
        <h2 id="signal-results-title" className="text-lg font-semibold text-brand-800">Results</h2>
        <p>
          Signal results use the <strong>same row format</strong> as the sequence branch, so you will get
          the same table, the same track view and the same CSV file. The differences: <Code>transcript_id</Code>{" "}
          and <Code>coverage</Code> (read depth at the site) are filled in, <Code>source</Code> is{" "}
          <Code>signal</Code>, and <Code>p_value</Code> may be empty where the signal model does not
          define one. Probabilities from the two branches are not calibrated against each other.
        </p>
      </section>

      <section aria-labelledby="signal-meanwhile-title" className="space-y-2">
        <h2 id="signal-meanwhile-title" className="text-lg font-semibold text-brand-800">In the meantime</h2>
        <p>
          Use the <Link to="/" className="text-brand-600 underline underline-offset-2">Sequence</Link> page
          for sequence-based predictions, the{" "}
          <a href="/docs" className="text-brand-600 underline underline-offset-2">API docs</a> for
          scripted access, and the{" "}
          <Link to="/help#phase2" className="text-brand-600 underline underline-offset-2">Help</Link> page
          for the details of the planned branch.
        </p>
      </section>
    </article>
  );
}
