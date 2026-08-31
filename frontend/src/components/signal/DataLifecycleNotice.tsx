/**
 * Limits and data-lifecycle statement of the signal branch, rendered from
 * GET /api/capabilities so the numbers can never drift from the server's configuration.
 * Shown on the upload page and on every result page.
 */
import type { Capabilities } from "../../api/types";
import { uploadTtlHours } from "../layout/CapabilitiesProvider";

export function DataLifecycleNotice({
  capabilities,
  compact = false,
}: {
  capabilities: Capabilities;
  compact?: boolean;
}) {
  const { limits, retention } = capabilities;
  const bamGb = limits.max_bam_gb ?? limits.max_pod5_gb;
  const ttlH = uploadTtlHours(capabilities);
  return (
    <aside
      data-testid="data-lifecycle"
      aria-labelledby="data-lifecycle-title"
      className="rounded border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950"
    >
      <h2 id="data-lifecycle-title" className="font-semibold">
        Limits and what happens to your data
      </h2>
      <ul className="mt-1.5 list-disc space-y-1 pl-5">
        {!compact && (
          <li>
            <strong>Size limits:</strong> pod5 up to {limits.max_pod5_gb} GB, BAM up to {bamGb} GB,
            reference FASTA up to {limits.max_reference_mb} MB, regions CSV up to{" "}
            {limits.max_regions.toLocaleString("en-US")} rows. Larger pod5 files: see{" "}
            <em>My pod5 is too big</em> below.
          </li>
        )}
        <li>
          <strong>Fair use:</strong> {limits.max_running_per_ip} running and {limits.max_queued_per_ip}{" "}
          queued job{limits.max_queued_per_ip === 1 ? "" : "s"} per network address at a time; a job is
          stopped after {limits.job_timeout_h} h.
        </li>
        <li>
          <strong>Inputs:</strong> uploaded files are used only for this job. The pod5 and the BAM are
          deleted {retention.inputs_deleted}; the reference and the regions file go with the job.
        </li>
        <li>
          <strong>Results:</strong> kept for {retention.results_days} days after the job finished, then
          deleted; an upload that is not completed within {ttlH} h is removed. The result page URL is the
          only key to a job: there is no account, so anyone with the link can see the results — bookmark
          it and share it deliberately.
        </li>
        <li>
          <strong>In your browser:</strong> no cookies. To resume an interrupted upload after a reload,
          the page keeps a small first-party record (file name, size, upload URL) in local storage for
          at most {ttlH} h; nothing is sent anywhere else.
        </li>
      </ul>
    </aside>
  );
}
