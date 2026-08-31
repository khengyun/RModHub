/** Shown when at least one called site has coverage below the low-coverage threshold (30 reads). */
export function CoverageWarning({ count, total, threshold }: { count: number; total: number; threshold: number }) {
  if (count <= 0) return null;
  return (
    <div
      data-testid="coverage-warning"
      role="note"
      className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950"
    >
      <strong>
        {count.toLocaleString("en-US")} of {total.toLocaleString("en-US")} sites have coverage below{" "}
        {threshold} reads.
      </strong>{" "}
      Sites called from fewer than {threshold} reads are unreliable: the modification rate rests on a
      handful of per-read calls and its confidence interval is wide. Treat them as tentative, sort the
      table by coverage, and open the read-level calls before drawing conclusions. (Coverage here counts
      the reads that received a score at that base, which is fewer than the raw read depth.)
    </div>
  );
}
