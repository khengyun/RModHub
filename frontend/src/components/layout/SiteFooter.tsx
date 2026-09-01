import {
  APP_VERSION,
  DIRECTRM_COPYRIGHT,
  DIRECTRM_MODEL_NAME,
  DIRECTRM_PAPER_URL,
  DIRECTRM_REPO_URL,
  MIT_LICENSE_URL,
  MODEL_COPYRIGHT,
  MODEL_NAME,
  MULTIRM_REPO_URL,
  PAPER_URL,
} from "./about";
import { ExtLink } from "./ExtLink";

function Sep() {
  return (
    <span aria-hidden className="text-slate-300">
      ·
    </span>
  );
}

/**
 * License footer, rendered on every page by Layout. The NAR Web Server Issue requires
 * the license to be visible on the landing page; this footer (plus LicenseNotice on the
 * Sequence and Nanopore signal pages) satisfies that. Both bundled models are credited.
 */
export function SiteFooter() {
  return (
    <footer data-testid="footer-license" className="border-t border-slate-200 bg-white">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-2 gap-y-1 px-4 py-4 text-xs text-slate-600">
        <span>
          <span className="font-semibold text-slate-800">RModHub</span> —{" "}
          <ExtLink href={MIT_LICENSE_URL}>MIT License</ExtLink>
        </span>
        <Sep />
        <span>
          Models: <ExtLink href={MULTIRM_REPO_URL}>{MODEL_NAME}</ExtLink> {MODEL_COPYRIGHT}, MIT (
          <ExtLink href={PAPER_URL}>Song <i>et al.</i> 2021</ExtLink>);{" "}
          <ExtLink href={DIRECTRM_REPO_URL}>{DIRECTRM_MODEL_NAME}</ExtLink> {DIRECTRM_COPYRIGHT}, MIT (
          <ExtLink href={DIRECTRM_PAPER_URL}>Zhang <i>et al.</i> 2025</ExtLink>; bundles Remora under the
          ONT Public License 1.0, research use)
        </span>
        <Sep />
        <span>No account, no cookies, no tracking</span>
        <Sep />
        <span>Version {APP_VERSION}</span>
      </div>
    </footer>
  );
}
