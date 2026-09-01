/**
 * Filter toolbar above the results table: modification-type chips, numeric limits,
 * quick text filter, reset. Purely controlled — the parent owns the `FilterInputs`.
 */
import { useId } from "react";
import type { PredictionMeta } from "../../api/types";
import { modTypeInfo } from "../../lib/modTypes";
import { tint } from "./ModTypeBadge";
import type { FilterInputs } from "./resultsModel";

export interface ResultsToolbarProps {
  inputs: FilterInputs;
  onChange: (patch: Partial<FilterInputs>) => void;
  onReset: () => void;
  /** Chips to show, in order (the 12 canonical types + any unexpected one). */
  modTypes: readonly string[];
  /** Rows per type over the unfiltered result set (missing = 0 -> chip disabled). */
  counts: ReadonlyMap<string, number>;
  meta: PredictionMeta;
}

const numberInputClass =
  "w-20 rounded border border-slate-300 bg-white px-1.5 py-1 text-right text-sm tabular-nums focus:border-brand-600 focus:outline-none";

export function ResultsToolbar(p: ResultsToolbarProps) {
  const chipsId = useId();
  const activeCount = p.modTypes.filter((t) => p.inputs.modTypes.has(t)).length;
  // Signal rows have no p-value (the column is hidden too) and "probability" is a rate.
  const signal = p.meta.source === "signal";

  const setModTypes = (next: ReadonlySet<string>) => p.onChange({ modTypes: next });
  const toggle = (id: string) => {
    const next = new Set(p.inputs.modTypes);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setModTypes(next);
  };

  return (
    <div data-testid="results-toolbar" className="space-y-3 rounded border border-slate-200 bg-white px-4 py-3 text-sm">
      {/* Modification-type chips */}
      <div>
        <div className="mb-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span id={chipsId} className="font-medium">
            Modification{" "}
            <span className="font-normal text-slate-500">
              ({activeCount}/{p.modTypes.length} selected)
            </span>
          </span>
          <button
            type="button"
            data-testid="filter-mod-type-all"
            onClick={() => setModTypes(new Set(p.modTypes))}
            className="text-brand-600 underline-offset-2 hover:underline"
          >
            All
          </button>
          <button
            type="button"
            data-testid="filter-mod-type-none"
            onClick={() => setModTypes(new Set())}
            className="text-brand-600 underline-offset-2 hover:underline"
          >
            None
          </button>
        </div>
        <div role="group" aria-labelledby={chipsId} className="flex flex-wrap gap-1.5">
          {p.modTypes.map((id) => {
            const info = modTypeInfo(id);
            const count = p.counts.get(id) ?? 0;
            const active = p.inputs.modTypes.has(id);
            return (
              <button
                key={id}
                type="button"
                data-testid={`filter-mod-type-${id}`}
                aria-pressed={active}
                disabled={count === 0}
                onClick={() => toggle(id)}
                title={info.description}
                className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                  active ? "text-slate-900" : "border-slate-300 bg-white text-slate-500 hover:bg-slate-50"
                }`}
                style={active ? { borderColor: info.color, backgroundColor: tint(info.color) } : undefined}
              >
                <span
                  aria-hidden
                  className="inline-block h-2 w-2 shrink-0 rounded-full"
                  style={{ backgroundColor: active ? info.color : "#cbd5e1" }}
                />
                <span className="font-medium">{info.label}</span>
                <span className="tabular-nums text-slate-500">{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Numeric limits + quick filter */}
      <div className="flex flex-wrap items-end gap-x-5 gap-y-2">
        {!signal && (
          <label className="flex items-center gap-1.5">
            <span className="text-slate-600">p-value ≤</span>
            <input
              data-testid="filter-pvalue-max"
              type="number"
              inputMode="decimal"
              min={0}
              max={1}
              step={0.001}
              value={p.inputs.pMax}
              onChange={(e) => p.onChange({ pMax: e.target.value })}
              className={numberInputClass}
            />
          </label>
        )}
        <label className="flex items-center gap-1.5">
          <span className="text-slate-600">{signal ? "Rate ≥" : "Probability ≥"}</span>
          <input
            data-testid="filter-prob-min"
            type="number"
            inputMode="decimal"
            min={0}
            max={1}
            step={0.01}
            value={p.inputs.probMin}
            onChange={(e) => p.onChange({ probMin: e.target.value })}
            className={numberInputClass}
          />
        </label>
        <div className="flex items-center gap-1.5">
          <span className="text-slate-600">Position</span>
          <input
            data-testid="filter-pos-min"
            aria-label="Minimum position"
            type="number"
            inputMode="numeric"
            min={1}
            max={p.meta.sequence_length}
            step={1}
            value={p.inputs.posMin}
            onChange={(e) => p.onChange({ posMin: e.target.value })}
            className={numberInputClass}
          />
          <span className="text-slate-500">–</span>
          <input
            data-testid="filter-pos-max"
            aria-label="Maximum position"
            type="number"
            inputMode="numeric"
            min={1}
            max={p.meta.sequence_length}
            step={1}
            value={p.inputs.posMax}
            onChange={(e) => p.onChange({ posMax: e.target.value })}
            className={numberInputClass}
          />
        </div>
        <label className="flex items-center gap-1.5">
          <span className="text-slate-600">Find</span>
          <input
            data-testid="filter-text"
            type="search"
            value={p.inputs.text}
            onChange={(e) => p.onChange({ text: e.target.value })}
            placeholder="position or type, e.g. 79 or m5C"
            className="w-56 rounded border border-slate-300 bg-white px-2 py-1 text-sm focus:border-brand-600 focus:outline-none"
          />
        </label>
        <button
          type="button"
          data-testid="filter-reset"
          onClick={p.onReset}
          className="rounded border border-slate-300 bg-white px-3 py-1 text-sm hover:bg-slate-50"
        >
          Reset filters
        </button>
      </div>
    </div>
  );
}
