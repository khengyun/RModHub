/**
 * Sequence-branch model picker and the comparison view it unlocks. The API is stubbed at
 * `fetch`; what matters here is which `models` the page sends and how it renders several
 * runs, not the model output itself.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import capabilities from "../api/fixtures/capabilities.json";
import type { Capabilities, ModSite, PredictionMeta } from "../api/types";
import {
  CapabilitiesProvider,
  type CapabilitiesState,
} from "../components/layout/CapabilitiesProvider";
import { SequencePage } from "./SequencePage";

const SEQ = "ACGU".repeat(40); // 160 nt, past the 51-nt minimum

const MODELS = [
  { id: "multirm", label: "MultiRM", description: "d1", default: true, name: "MultiRM", version: "v1" },
  { id: "other", label: "Other", description: "d2", default: false, name: "Other", version: "v2" },
];

function state(models: typeof MODELS | undefined): CapabilitiesState {
  return {
    status: "ready",
    capabilities: { ...(capabilities as Capabilities), sequence_models: models },
  };
}

function meta(over: Partial<PredictionMeta> = {}): PredictionMeta {
  return {
    sequence_length: 160,
    predicted_start: 26,
    predicted_end: 135,
    alpha: 0.05,
    n_sites: 0,
    model_name: "MultiRM",
    model_version: "v1",
    inference_ms: 10,
    source: "sequence",
    transcript_id: null,
    mod_types: ["m6A"],
    note: "",
    extra: {},
    attention: null,
    ...over,
  };
}

function site(position: number, mod_type = "m6A"): ModSite {
  return {
    transcript_id: null,
    position,
    mod_type,
    probability: 0.9,
    p_value: 0.01,
    coverage: null,
    source: "sequence",
  };
}

/** Records every POST body so a test can assert on the `models` field. */
let sent: Record<string, unknown>[] = [];

const SAMPLES = [
  { name: "short", description: "d", sequence: "ACGT".repeat(40), length: 160, source_url: "u" },
  { name: "long", description: "d", sequence: "ACGT".repeat(300), length: 1200, source_url: "u" },
];

function stubFetch(response: unknown, catalog: unknown = []) {
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const json = (b: unknown) =>
        new Response(JSON.stringify(b), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (String(url).startsWith("/api/samples/sequence/catalog")) return json(catalog);
      if (String(url).startsWith("/api/samples/sequence")) {
        const name = new URL(url, "http://x").searchParams.get("name");
        return json(SAMPLES.find((s) => s.name === name) ?? SAMPLES[0]);
      }
      if (String(url).startsWith("/api/predict/sequence")) {
        sent.push(JSON.parse(String(init?.body)));
        return json(response);
      }
      return json({});
    }),
  );
}

async function typeAndRun(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByRole("textbox"), SEQ);
  await user.click(screen.getByRole("button", { name: /predict/i }));
}

function renderPage(models: typeof MODELS | undefined) {
  return render(
    <MemoryRouter>
      <CapabilitiesProvider value={state(models)}>
        <SequencePage />
      </CapabilitiesProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe("model picker", () => {
  it("is hidden when the server reports no models", () => {
    stubFetch({ results: [], meta: meta() });
    renderPage(undefined);
    expect(screen.queryByTestId("model-picker")).toBeNull();
  });

  it("is hidden when the server reports a single model", () => {
    stubFetch({ results: [], meta: meta() });
    renderPage([MODELS[0]]);
    expect(screen.queryByTestId("model-picker")).toBeNull();
  });

  it("omits `models` from the request when there is nothing to choose", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() });
    renderPage([MODELS[0]]);
    await typeAndRun(user);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).not.toHaveProperty("models");
  });

  it("starts on the default model and sends it", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() });
    renderPage(MODELS);
    expect(screen.getByTestId("model-multirm")).toBeChecked();
    expect(screen.getByTestId("model-other")).not.toBeChecked();
    await typeAndRun(user);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].models).toEqual(["multirm"]);
  });

  it("sends every ticked model, in the server's order", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() });
    renderPage(MODELS);
    await user.click(screen.getByTestId("model-other"));
    await typeAndRun(user);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].models).toEqual(["multirm", "other"]);
  });

  it("refuses to untick the last model", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() });
    renderPage(MODELS);
    await user.click(screen.getByTestId("model-multirm"));
    expect(screen.getByTestId("model-multirm")).toBeChecked();
  });
});

describe("comparison view", () => {
  const COMPARISON = {
    results: [site(30), site(40)],
    meta: meta({ n_sites: 2 }),
    comparison: [
      { model: "multirm", results: [site(30), site(40)], meta: meta({ n_sites: 2 }) },
      {
        model: "other",
        results: [site(40), site(50)],
        meta: meta({ n_sites: 2, model_name: "Other", model_version: "v2" }),
      },
    ],
  };

  it("is absent for a single-model response", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [site(30)], meta: meta({ n_sites: 1 }) });
    renderPage(MODELS);
    await typeAndRun(user);
    await screen.findByTestId("results");
    expect(screen.queryByTestId("comparison")).toBeNull();
  });

  it("counts the sites every model agreed on", async () => {
    const user = userEvent.setup();
    stubFetch(COMPARISON);
    renderPage(MODELS);
    await user.click(screen.getByTestId("model-other"));
    await typeAndRun(user);
    const panel = await screen.findByTestId("comparison");
    // position 40 is in both runs; 30 and 50 are model-specific.
    expect(within(panel).getByText("1", { selector: "strong" })).toBeInTheDocument();
    const rows = within(panel).getAllByRole("row").slice(1);
    // cells: [positions scored, sites, only-this-model, time, show]
    expect(within(rows[0]).getAllByRole("cell")[2]).toHaveTextContent("1");
    expect(within(rows[1]).getAllByRole("cell")[2]).toHaveTextContent("1");
  });

  it("shows how many positions each model could score", async () => {
    const user = userEvent.setup();
    stubFetch(COMPARISON);
    renderPage(MODELS);
    await user.click(screen.getByTestId("model-other"));
    await typeAndRun(user);
    const panel = await screen.findByTestId("comparison");
    const rows = within(panel).getAllByRole("row").slice(1);
    // meta says 26..135 of a 160-nt input, i.e. 110 scorable positions.
    expect(within(rows[0]).getAllByRole("cell")[0]).toHaveTextContent("110");
    expect(within(rows[0]).getAllByRole("cell")[0]).toHaveTextContent("26");
  });

  it("shows the first run and switches when another is picked", async () => {
    const user = userEvent.setup();
    stubFetch(COMPARISON);
    renderPage(MODELS);
    await user.click(screen.getByTestId("model-other"));
    await typeAndRun(user);
    await screen.findByTestId("comparison");
    expect(screen.getByTestId("show-multirm")).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByTestId("show-other"));
    expect(screen.getByTestId("show-other")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("summary")).toHaveTextContent("Other");
  });
});

describe("length-aware picker", () => {
  const LONG = [
    { ...MODELS[0], min_sequence_nt: 51 },
    { ...MODELS[1], min_sequence_nt: 601, max_sequence_nt: 2000 },
  ];

  it("disables a model whose window does not fit the input", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() });
    renderPage(LONG);
    await user.type(screen.getByRole("textbox"), SEQ); // 160 nt, short of 601
    expect(screen.getByTestId("model-other")).toBeDisabled();
    expect(screen.getByTestId("model-other-blocked")).toHaveTextContent("at least 601 nt");
    expect(screen.getByTestId("model-multirm")).toBeEnabled();
  });

  it("never sends a model the input cannot feed", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() });
    renderPage(LONG);
    await typeAndRun(user);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].models).toEqual(["multirm"]);
  });

  it("leaves every model selectable before anything is typed", () => {
    stubFetch({ results: [], meta: meta() });
    renderPage(LONG);
    expect(screen.getByTestId("model-other")).toBeEnabled();
    expect(screen.queryByTestId("model-other-blocked")).toBeNull();
  });
});

describe("sample buttons", () => {
  it("keeps one button when the server offers a single sample", async () => {
    stubFetch({ results: [], meta: meta() }, [SAMPLES[0]]);
    renderPage(MODELS);
    await waitFor(() => expect(screen.getByTestId("load-sample")).toBeInTheDocument());
    expect(screen.queryByTestId("load-sample-long")).toBeNull();
  });

  it("shows one button per sample, labelled by length", async () => {
    stubFetch({ results: [], meta: meta() }, SAMPLES);
    renderPage(MODELS);
    const second = await screen.findByTestId("load-sample-long");
    expect(screen.getByTestId("load-sample")).toHaveTextContent("160 nt");
    expect(second).toHaveTextContent("1,200 nt");
  });

  it("loads the sequence of the sample that was clicked", async () => {
    const user = userEvent.setup();
    stubFetch({ results: [], meta: meta() }, SAMPLES);
    renderPage(MODELS);
    await user.click(await screen.findByTestId("load-sample-long"));
    await waitFor(() =>
      expect(screen.getByRole("textbox")).toHaveValue(SAMPLES[1].sequence),
    );
  });
});
