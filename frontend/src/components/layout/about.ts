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
