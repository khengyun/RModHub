export function formatP(p: number | null): string {
  if (p === null) return "—";
  if (p === 0) return "< 0.0067"; // below 1/150 resolution
  return p < 0.001 ? p.toExponential(2) : p.toFixed(4);
}

export function formatProb(p: number): string {
  return p.toFixed(3);
}

export function formatMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}
