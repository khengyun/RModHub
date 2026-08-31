/**
 * Facts shown in the footer, the landing-page license notice and the Help page: one
 * source so the wording (license holders, citation, links) cannot drift between pages.
 *
 * The URLs are plain hyperlinks (navigation), never resources loaded by the page, and
 * every one of them must be on the allowlist in scripts/check-no-external-urls.mjs.
 * Keep them as full string literals (no template interpolation) so that check can
 * recognise them verbatim in the minified bundle.
 */

/** Keep in sync with package.json "version". */
export const APP_VERSION = "0.1.0";

/* ---------- MultiRM (sequence branch) ---------- */

export const MULTIRM_REPO_URL = "https://github.com/Tsedao/MultiRM";
/** Verified against the CrossRef DOI registry: Nat Commun 12, 4011 (2021). */
export const PAPER_DOI = "10.1038/s41467-021-24313-3";
export const PAPER_URL = "https://doi.org/10.1038/s41467-021-24313-3";
export const MIT_LICENSE_URL = "https://opensource.org/licenses/MIT";

export const MODEL_NAME = "MultiRM";
export const MODEL_COPYRIGHT = "© 2021 Zitao Song";

export const CITATION = {
  authors:
    "Song Z, Huang D, Song B, Chen K, Song Y, Liu G, Su J, de Magalhães JP, Rigden DJ, Meng J",
  title:
    "Attention-based multi-label neural networks for integrated prediction and interpretation of twelve widely occurring RNA modifications",
  journal: "Nature Communications",
  volume: "12",
  article: "4011",
  year: "2021",
  doi: PAPER_DOI,
} as const;

/** One-line citation for copy/paste. */
export const CITATION_TEXT =
  CITATION.authors +
  ". " +
  CITATION.title +
  ". " +
  CITATION.journal +
  " " +
  CITATION.volume +
  ", " +
  CITATION.article +
  " (" +
  CITATION.year +
  "). " +
  PAPER_URL;

/* ---------- DirectRM (nanopore signal branch) ---------- */

export const DIRECTRM_MODEL_NAME = "DirectRM";
export const DIRECTRM_REPO_URL = "https://github.com/yuxinPenny/DirectRM";
/** Vendored commit (docs/signal-branch.md section 2). */
export const DIRECTRM_COMMIT = "bc7a085";
export const DIRECTRM_COPYRIGHT = "© 2025 Yuxin Zhang";
export const DIRECTRM_PAPER_DOI = "10.1038/s41467-025-64495-8";
export const DIRECTRM_PAPER_URL = "https://doi.org/10.1038/s41467-025-64495-8";

export const DIRECTRM_CITATION = {
  authors: "Zhang Y, Wu Y, Ma J, et al.",
  title:
    "DirectRM: integrated detection of landscape and crosstalk between multiple RNA modifications using direct RNA sequencing",
  journal: "Nature Communications",
  volume: "16",
  article: "9450",
  year: "2025",
  doi: DIRECTRM_PAPER_DOI,
} as const;

export const DIRECTRM_CITATION_TEXT =
  DIRECTRM_CITATION.authors +
  " " +
  DIRECTRM_CITATION.title +
  ". " +
  DIRECTRM_CITATION.journal +
  " " +
  DIRECTRM_CITATION.volume +
  ", " +
  DIRECTRM_CITATION.article +
  " (" +
  DIRECTRM_CITATION.year +
  "). " +
  DIRECTRM_PAPER_URL;

/** Third-party components bundled with the signal worker, disclosed on the landing page. */
export const DIRECTRM_THIRD_PARTY = [
  { name: "DirectRM", license: "MIT License", holder: DIRECTRM_COPYRIGHT },
  { name: "ONT k-mer level tables (kmer_models)", license: "MPL-2.0", holder: "Oxford Nanopore Technologies" },
  {
    name: "Remora",
    license: "Oxford Nanopore Technologies Public License 1.0 (research use only)",
    holder: "Oxford Nanopore Technologies",
  },
] as const;
