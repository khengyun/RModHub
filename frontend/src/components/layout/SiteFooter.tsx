import type { HealthResponse } from "../../api/types";
import {
  APP_VERSION,
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
 * Sequence page) satisfies that.
 */
export function SiteFooter({ api }: { api: HealthResponse | null }) {
  return (
    <footer data-testid="footer-license" className="border-t border-slate-200 bg-white">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-2 gap-y-1 px-4 py-4 text-xs text-slate-600">
        <span>
          <span className="font-semibold text-slate-800">RModHub</span> —{" "}
          <ExtLink href={MIT_LICENSE_URL}>MIT License</ExtLink>
        </span>
        <Sep />
        <span>
          Model: <ExtLink href={MULTIRM_REPO_URL}>{MODEL_NAME}</ExtLink> {MODEL_COPYRIGHT},{" "}
          <ExtLink href={MIT_LICENSE_URL}>MIT</ExtLink>
        </span>
        <Sep />
        <span>
          <ExtLink href={PAPER_URL}>Citation</ExtLink>: Song <i>et al.</i>, Nat Commun 2021
        </span>
        <Sep />
        <span>No account, no cookies, no tracking</span>
        <Sep />
        <span>
          Version {APP_VERSION}
          {api ? ` · API ${api.version} · ${api.model_name} ${api.model_version}` : ""}
        </span>
      </div>
    </footer>
  );
}
