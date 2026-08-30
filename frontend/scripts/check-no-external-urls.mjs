#!/usr/bin/env node
/**
 * Fails the build if dist/ references any external resource (CDN scripts, fonts, styles,
 * images, fetch targets). NAR Web Server Issue rule: no third-party assets / cookies.
 *
 * Plain hyperlinks to the model's source repository, its paper and the license text are
 * allowed (they are navigation, not resources loaded by the page) but must be listed here
 * explicitly. Everything else that matches https?:// is an error.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const DIST = process.argv[2] ?? "dist";
const ALLOWED_HYPERLINKS = [
  "https://github.com/Tsedao/MultiRM",
  "https://doi.org/10.1038/s41467-021-24313-3", // MultiRM paper, Nat Commun 2021
  "https://opensource.org/licenses/MIT",
  "https://academic.oup.com/nar", // journal link on the About/Help page, if used
];
// Strings that live inside library code but are never fetched:
//  - XML namespace identifiers (React DOM, inline SVG),
//  - documentation links embedded in React / React Router *error messages*.
// The E2E suite proves the point at runtime by recording every network request and
// asserting none leaves the app's own origin.
const ALLOWED_NAMESPACES = ["http://www.w3.org/"];
const ALLOWED_MESSAGE_LINKS = ["https://react.dev/errors/", "https://reactrouter.com/"];

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else out.push(p);
  }
  return out;
}

const files = walk(DIST).filter((f) => /\.(html|js|css|svg|json|txt|webmanifest)$/.test(f));
const re = /https?:\/\/[^\s"'`)<>\\]+/g;
const violations = [];
const links = new Map();
for (const f of files) {
  const text = readFileSync(f, "utf8");
  for (const m of text.matchAll(re)) {
    const url = m[0].replace(/[.,;:]+$/, "");
    if (/^https?:\/\/(localhost|127\.0\.0\.1)/.test(url)) continue;
    if (ALLOWED_NAMESPACES.some((ns) => url.startsWith(ns))) continue;
    if (ALLOWED_MESSAGE_LINKS.some((ns) => url.startsWith(ns))) continue;
    if (ALLOWED_HYPERLINKS.some((ok) => url === ok || url.startsWith(ok + "/") || url.startsWith(ok + "#"))) {
      links.set(url, (links.get(url) ?? 0) + 1);
      continue;
    }
    violations.push(`${relative(process.cwd(), f)}: ${url}`);
  }
}

if (links.size) {
  console.log("Allowed hyperlinks (navigation only, not loaded resources):");
  for (const [u, n] of links) console.log(`  ${u}  x${n}`);
}
if (violations.length) {
  console.error("\nEXTERNAL RESOURCE REFERENCES FOUND (not allowed):");
  for (const v of violations) console.error("  " + v);
  process.exit(1);
}
console.log(`\nOK: no external resources referenced in ${files.length} files under ${DIST}/`);
