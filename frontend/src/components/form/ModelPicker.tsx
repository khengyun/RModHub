/**
 * Sequence-branch model picker. Rendered only when the server reports two or more models
 * in GET /api/capabilities, so a single-model deployment looks exactly as it did before.
 *
 * Ticking several models runs each of them on the same input and fills `comparison` in the
 * response; at least one must stay ticked (unticking the last one is a no-op). A model
 * whose window does not fit the current input is disabled with the reason spelled out,
 * rather than letting the request come back as a 422.
 */
import type { SequenceModelInfo } from "../../api/types";

/** Why this model cannot run on an input of `length` nt, or null when it can. */
export function lengthBlocker(model: SequenceModelInfo, length: number): string | null {
  const min = model.min_sequence_nt;
  const max = model.max_sequence_nt;
  if (length === 0) return null; // nothing typed yet: leave every model selectable
  if (typeof min === "number" && length < min) {
    return `needs at least ${min.toLocaleString("en-US")} nt`;
  }
  if (typeof max === "number" && length > max) {
    return `accepts at most ${max.toLocaleString("en-US")} nt`;
  }
  return null;
}

export function ModelPicker({
  models,
  selected,
  onChange,
  sequenceLength,
  disabled,
}: {
  models: SequenceModelInfo[];
  selected: string[];
  onChange: (ids: string[]) => void;
  /** Normalised length of what is in the textarea, so unusable models can be greyed out. */
  sequenceLength: number;
  disabled?: boolean;
}) {
  if (models.length < 2) return null;

  const toggle = (id: string) => {
    const next = selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id];
    if (next.length === 0) return; // always keep one model running
    // Keep the server's order so `comparison` and the tabs line up with the picker.
    onChange(models.filter((m) => next.includes(m.id)).map((m) => m.id));
  };

  return (
    <fieldset data-testid="model-picker" className="rounded border border-slate-200 bg-white px-4 py-3">
      <legend className="px-1 text-sm font-medium text-slate-700">Model</legend>
      <p className="mb-2 text-xs text-slate-500">
        Tick more than one to score the same sequence with each and compare the results.
      </p>
      <div className="space-y-2">
        {models.map((m) => {
          const blocked = lengthBlocker(m, sequenceLength);
          return (
            <label
              key={m.id}
              className={`flex items-start gap-2 text-sm ${blocked ? "opacity-60" : ""}`}
            >
              <input
                type="checkbox"
                data-testid={`model-${m.id}`}
                className="mt-1"
                checked={selected.includes(m.id) && !blocked}
                disabled={disabled || blocked !== null}
                onChange={() => toggle(m.id)}
              />
              <span>
                <span className="font-medium">{m.label}</span>{" "}
                <span className="text-slate-500">
                  {m.name} {m.version}
                </span>
                <span className="block text-xs text-slate-500">{m.description}</span>
                {blocked && (
                  <span data-testid={`model-${m.id}-blocked`} className="block text-xs text-amber-700">
                    Not available for this input: {blocked}.
                  </span>
                )}
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}
