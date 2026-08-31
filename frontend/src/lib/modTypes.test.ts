import { describe, expect, it } from "vitest";
import { MOD_TYPES } from "../api/types";
import { EXTRA_MOD_TYPE_INFO, MOD_TYPE_INFO, MOD_TYPE_LIST, modTypeInfo, SIGNAL_MOD_TYPES } from "./modTypes";

describe("modTypes", () => {
  it("keeps the frozen 12 in canonical order", () => {
    expect(MOD_TYPE_LIST.map((m) => m.id)).toEqual([...MOD_TYPES]);
    expect(MOD_TYPE_LIST).toHaveLength(12);
  });

  it("knows ac4C (base C, N4-acetylcytidine) with a colour distinct from the 12", () => {
    const ac4c = modTypeInfo("ac4C");
    expect(ac4c).toBe(EXTRA_MOD_TYPE_INFO.ac4C);
    expect(ac4c.label).toBe("ac4C");
    expect(ac4c.base).toBe("C");
    expect(ac4c.description).toMatch(/N4-acetylcytidine/);
    const colours = new Set(Object.values(MOD_TYPE_INFO).map((m) => m.color.toLowerCase()));
    expect(colours.size).toBe(12);
    expect(colours.has(ac4c.color.toLowerCase())).toBe(false);
    expect(ac4c.color).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it("DirectRM's six types all resolve (Psi via the shared entry) and unknown ids fall back", () => {
    expect(SIGNAL_MOD_TYPES).toEqual(["ac4C", "m1A", "m5C", "m6A", "m7G", "Psi"]);
    for (const id of SIGNAL_MOD_TYPES) {
      expect(modTypeInfo(id).description).not.toBe("Unknown modification type.");
    }
    expect(modTypeInfo("Psi")).toBe(MOD_TYPE_INFO.Psi);
    const unknown = modTypeInfo("pseU");
    expect(unknown.label).toBe("pseU");
    expect(unknown.description).toBe("Unknown modification type.");
  });
});
