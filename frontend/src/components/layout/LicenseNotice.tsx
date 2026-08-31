/**
 * Compact "about + license" block for the landing pages (Sequence and Nanopore signal).
 * Self-contained: what the server does, who made the models, licenses, citations, the
 * "predictions, not evidence" caveat and the data statement (numbers from /api/capabilities).
 */
import { Link } from "react-router-dom";
import {
  CITATION,
  DIRECTRM_CITATION,
  DIRECTRM_COPYRIGHT,
  DIRECTRM_MODEL_NAME,
  DIRECTRM_PAPER_URL,
  DIRECTRM_REPO_URL,
  MIT_LICENSE_URL,
  MODEL_COPYRIGHT,
  MULTIRM_REPO_URL,
  PAPER_URL,
} from "./about";
import { useCapabilities } from "./CapabilitiesProvider";
import { ExtLink } from "./ExtLink";

export function LicenseNotice() {
  const { capabilities } = useCapabilities();
  const { retention } = capabilities;
  return (
    <section
      data-testid="license-notice"
      aria-labelledby="license-notice-title"
      className="rounded border border-slate-200 bg-white px-4 py-4 text-sm text-slate-700"
    >
      <h2 id="license-notice-title" className="font-semibold text-brand-800">
        About RModHub, licenses and citation
      </h2>
      <div className="mt-2 grid gap-x-8 gap-y-2 md:grid-cols-2">
        <p>
          <strong>What it does.</strong> RModHub has two branches. The <em>Sequence</em> branch scores a
          pasted RNA or DNA sequence (51–10,000 nt) for 12 RNA modification types with the MultiRM
          deep-learning model and lists the sites whose empirical p-value passes your significance
          level. The <em>Nanopore signal</em> branch calls m6A, m5C, m1A, m7G, Ψ and ac4C from the raw
          signal of Oxford Nanopore direct-RNA reads with DirectRM and reports per-site modification
          rates with confidence intervals. Both give a track view, a table and a CSV file. Details in
          the{" "}
          <Link to="/help" className="text-brand-600 underline underline-offset-2">
            Help
          </Link>
          .
        </p>
        <p>
          <strong>Predictions, not evidence.</strong> Every site is a computational prediction: from
          sequence alone (MultiRM, per-site p-values not corrected for multiple testing) or from a
          neural network's reading of the signal of a limited number of reads (DirectRM). Treat hits as
          candidates for experimental validation, not as proof of modification.
        </p>
        <p>
          <strong>Models.</strong> <ExtLink href={MULTIRM_REPO_URL}>MultiRM</ExtLink> by Zitao Song and
          colleagues (Song <i>et al.</i>, 2021), {MODEL_COPYRIGHT}, MIT License.{" "}
          <ExtLink href={DIRECTRM_REPO_URL}>{DIRECTRM_MODEL_NAME}</ExtLink> by Yuxin Zhang and
          colleagues (Zhang <i>et al.</i>, 2025), {DIRECTRM_COPYRIGHT}, MIT License. RModHub serves the
          published weights and runs the original code unmodified.
        </p>
        <p>
          <strong>Licenses.</strong> RModHub server and web interface:{" "}
          <ExtLink href={MIT_LICENSE_URL}>MIT License</ExtLink>. Bundled: MultiRM (MIT, {MODEL_COPYRIGHT});
          DirectRM (MIT, {DIRECTRM_COPYRIGHT}); ONT k-mer level tables (MPL-2.0); Remora (Oxford
          Nanopore Technologies Public License 1.0, <em>research use only</em>); Swagger UI (Apache-2.0).
        </p>
        <p className="md:col-span-2">
          <strong>Your data.</strong> No account, no cookies, no tracking. Submitted sequences are not
          stored. Nanopore uploads are used only for your job: pod5 and BAM are deleted{" "}
          {retention.inputs_deleted}, results after {retention.results_days} days; the result link is the
          only key to a job.
        </p>
        <p className="md:col-span-2">
          <strong>Cite.</strong> {CITATION.authors}. {CITATION.title}. <i>{CITATION.journal}</i>{" "}
          {CITATION.volume}, {CITATION.article} ({CITATION.year}).{" "}
          <ExtLink href={PAPER_URL}>doi:{CITATION.doi}</ExtLink> — and, for signal results,{" "}
          {DIRECTRM_CITATION.authors} {DIRECTRM_MODEL_NAME}. <i>{DIRECTRM_CITATION.journal}</i>{" "}
          {DIRECTRM_CITATION.volume}, {DIRECTRM_CITATION.article} ({DIRECTRM_CITATION.year}).{" "}
          <ExtLink href={DIRECTRM_PAPER_URL}>doi:{DIRECTRM_CITATION.doi}</ExtLink>
        </p>
      </div>
    </section>
  );
}
