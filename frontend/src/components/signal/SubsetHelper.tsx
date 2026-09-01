/**
 * "My pod5 is too big": the copy-paste command of the subset tool (tools/, Docker image
 * rmodhub/subset:local) and a size estimate for the subset file.
 */
import { useId, useState } from "react";
import { estimateSubsetMb, GB, SUBSET_DOCKER_COMMAND } from "./uploadModel";

function copyToClipboard(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for insecure contexts / older browsers.
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } finally {
      ta.remove();
    }
    if (ok) resolve();
    else reject(new Error("copy failed"));
  });
}

export function SubsetHelper({ maxPod5Gb }: { maxPod5Gb: number }) {
  const [copied, setCopied] = useState<"idle" | "ok" | "fail">("idle");
  const [pod5Gb, setPod5Gb] = useState("");
  const [totalReads, setTotalReads] = useState("");
  const [regionReads, setRegionReads] = useState("");
  const idA = useId();
  const idB = useId();
  const idC = useId();

  const estimate = estimateSubsetMb(Number(pod5Gb) * GB, Number(totalReads), Number(regionReads));
  const estimateText =
    pod5Gb === "" || totalReads === "" || regionReads === ""
      ? "Fill in the three numbers to get an estimate."
      : estimate === null
        ? "Enter positive numbers."
        : `≈ ${estimate >= 1024 ? `${(estimate / 1024).toFixed(2)} GB` : `${estimate.toLocaleString("en-US")} MB`} for the subset pod5${
            estimate > maxPod5Gb * 1024 ? " — still above the limit; use fewer regions." : "."
          }`;

  const copy = async () => {
    try {
      await copyToClipboard(SUBSET_DOCKER_COMMAND);
      setCopied("ok");
    } catch {
      setCopied("fail");
    }
    setTimeout(() => setCopied("idle"), 2000);
  };

  return (
    <details data-testid="subset-panel" className="rounded border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
      <summary className="cursor-pointer font-semibold text-brand-800">My pod5 is too big</summary>
      <div className="mt-3 space-y-3">
        <p>
          DirectRM only looks at reads that overlap your regions, so you can shrink a whole-run pod5
          to just those reads before uploading. The subset tool runs locally, needs Docker, and writes
          a small pod5 plus the matching BAM:
        </p>
        <div className="flex flex-wrap items-start gap-2">
          <pre className="max-w-full flex-1 overflow-x-auto rounded bg-slate-900 px-3 py-2 font-mono text-xs leading-5 text-slate-100">
            <code data-testid="subset-command">{SUBSET_DOCKER_COMMAND}</code>
          </pre>
          <button
            type="button"
            data-testid="subset-copy"
            onClick={() => void copy()}
            className="rounded border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium hover:bg-slate-50"
          >
            {copied === "ok" ? "Copied" : copied === "fail" ? "Copy failed" : "Copy command"}
          </button>
          <span className="sr-only" aria-live="polite">
            {copied === "ok" ? "Command copied to the clipboard" : ""}
          </span>
        </div>
        <ul className="list-disc space-y-1 pl-5 text-slate-600">
          <li>
            <code>-i</code> the big pod5 (or a directory of pod5 files), <code>-b</code> the sorted, indexed
            BAM produced with <code>dorado … --emit-moves</code>, <code>-r</code> the same regions CSV you
            will upload.
          </li>
          <li>
            Output: <code>small.pod5</code> and <code>small.bam</code> — upload those two together with the
            reference FASTA and the regions CSV. The image is built locally from <code>tools/</code> in the
            RModHub repository (see the Help page); nothing is downloaded from a third party.
          </li>
        </ul>

        <fieldset className="rounded border border-slate-200 px-3 py-2">
          <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Size estimate
          </legend>
          <div className="flex flex-wrap items-end gap-4">
            <label htmlFor={idA} className="flex flex-col text-xs text-slate-600">
              pod5 size (GB)
              <input
                id={idA}
                data-testid="subset-pod5-gb"
                type="number"
                min={0}
                step="any"
                value={pod5Gb}
                onChange={(e) => setPod5Gb(e.target.value)}
                className="mt-0.5 w-28 rounded border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label htmlFor={idB} className="flex flex-col text-xs text-slate-600">
              reads in the run
              <input
                id={idB}
                data-testid="subset-total-reads"
                type="number"
                min={0}
                step={1}
                value={totalReads}
                onChange={(e) => setTotalReads(e.target.value)}
                className="mt-0.5 w-32 rounded border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label htmlFor={idC} className="flex flex-col text-xs text-slate-600">
              reads in your regions
              <input
                id={idC}
                data-testid="subset-region-reads"
                type="number"
                min={0}
                step={1}
                value={regionReads}
                onChange={(e) => setRegionReads(e.target.value)}
                className="mt-0.5 w-32 rounded border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <p data-testid="subset-estimate" aria-live="polite" className="text-sm text-slate-800">
              {estimateText}
            </p>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            <code>samtools view -c in.bam</code> gives the total; <code>samtools view -c -L regions.bed in.bam</code>{" "}
            (after converting the CSV to BED) the reads in your regions. The estimate assumes the signal
            dominates the file size; DirectRM uses at most 150 reads per region anyway.
          </p>
        </fieldset>
      </div>
    </details>
  );
}
