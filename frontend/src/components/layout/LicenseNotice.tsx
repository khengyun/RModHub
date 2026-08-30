/**
 * Compact "about + license" block for the landing page. Mount it at the bottom of
 * SequencePage:  import { LicenseNotice } from "../components/layout/LicenseNotice";
 * Self-contained: what the server does, who made the model, license, citation and the
 * "predictions, not evidence" caveat.
 */
import { Link } from "react-router-dom";
import { CITATION, MIT_LICENSE_URL, MODEL_COPYRIGHT, MULTIRM_REPO_URL, PAPER_URL } from "./about";
import { ExtLink } from "./ExtLink";

export function LicenseNotice() {
  return (
    <section
      data-testid="license-notice"
      aria-labelledby="license-notice-title"
      className="rounded border border-slate-200 bg-white px-4 py-4 text-sm text-slate-700"
    >
      <h2 id="license-notice-title" className="font-semibold text-brand-800">
        About RModHub, license and citation
      </h2>
      <div className="mt-2 grid gap-x-8 gap-y-2 md:grid-cols-2">
        <p>
          <strong>What it does.</strong> RModHub scores a pasted RNA or DNA sequence (51–10,000 nt)
          for 12 RNA modification types with the MultiRM deep-learning model and lists the sites
          whose empirical p-value passes your significance level, as a track view, a table and a
          CSV file. A nanopore-signal branch (DirectRM) is planned. Details in the{" "}
          <Link to="/help" className="text-brand-600 underline underline-offset-2">
            Help
          </Link>
          .
        </p>
        <p>
          <strong>Predictions, not evidence.</strong> Every site is a computational prediction from
          sequence alone, with per-site p-values that are not corrected for multiple testing. Treat
          hits as candidates for experimental validation, not as proof of modification.
        </p>
        <p>
          <strong>Model.</strong> <ExtLink href={MULTIRM_REPO_URL}>MultiRM</ExtLink> by Zitao Song
          and colleagues (Song <i>et al.</i>, 2021), {MODEL_COPYRIGHT}, released under the MIT
          License. RModHub serves the published weights; the numerics are those of the original
          code.
        </p>
        <p>
          <strong>License.</strong> RModHub server and web interface:{" "}
          <ExtLink href={MIT_LICENSE_URL}>MIT License</ExtLink>. Bundled model MultiRM: MIT License,{" "}
          {MODEL_COPYRIGHT}. Bundled Swagger UI (API docs): Apache-2.0. No account, no cookies, no
          tracking; submitted sequences are not stored.
        </p>
        <p className="md:col-span-2">
          <strong>Cite.</strong> {CITATION.authors}. {CITATION.title}. <i>{CITATION.journal}</i>{" "}
          {CITATION.volume}, {CITATION.article} ({CITATION.year}).{" "}
          <ExtLink href={PAPER_URL}>doi:{CITATION.doi}</ExtLink>
        </p>
      </div>
    </section>
  );
}
