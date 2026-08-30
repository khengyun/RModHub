/**
 * Coloured pill for one modification type. Colours come from `modTypeInfo` (shared with the
 * track view); they are applied as inline colour styles because Tailwind cannot generate
 * classes for runtime values. Everything else is Tailwind.
 */
import { modTypeInfo } from "../../lib/modTypes";

/** 6-digit hex colour -> the same colour at ~12% opacity (for pill backgrounds). */
export function tint(hex: string): string {
  return /^#[0-9a-fA-F]{6}$/.test(hex) ? `${hex}1f` : "transparent";
}

export function ModTypeBadge({ id }: { id: string }) {
  const info = modTypeInfo(id);
  return (
    <span
      title={info.description}
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium text-slate-800"
      style={{ borderColor: info.color, backgroundColor: tint(info.color) }}
    >
      <span aria-hidden className="inline-block h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: info.color }} />
      {info.label}
    </span>
  );
}
