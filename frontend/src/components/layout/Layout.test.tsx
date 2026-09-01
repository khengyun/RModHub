import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router-dom";
import capabilitiesFixture from "../../api/fixtures/capabilities.json";
import type { Capabilities } from "../../api/types";
import { HelpPage, SIGNAL_SUBSECTIONS } from "../../pages/HelpPage";
import { SignalPage } from "../../pages/SignalPage";
import {
  CapabilitiesProvider,
  DEFAULT_CAPABILITIES,
  type CapabilitiesState,
} from "./CapabilitiesProvider";
import { Layout } from "./Layout";
import { LicenseNotice } from "./LicenseNotice";

/** Anchor ids the Help page must expose (UI contract with tooltips / E2E tests). */
const HELP_ANCHORS = [
  "quick-start", "input", "reading-results", "flanks", "mod-types", "multiple-mods",
  "track-view", "table-and-csv", "limits", "citation", "privacy", "nanopore-signal",
  ...SIGNAL_SUBSECTIONS.map((s) => s.id),
];

const ENABLED: CapabilitiesState = { status: "ready", capabilities: capabilitiesFixture as Capabilities };
const DISABLED: CapabilitiesState = { status: "ready", capabilities: DEFAULT_CAPABILITIES };

/** Every hyperlink must point at one of the two model repos, their papers or the MIT text. */
const ALLOWED_LINK =
  /^https:\/\/(github\.com\/Tsedao\/MultiRM|doi\.org\/10\.1038\/s41467-021-24313-3|github\.com\/yuxinPenny\/DirectRM|doi\.org\/10\.1038\/s41467-025-64495-8|opensource\.org\/licenses\/MIT)/;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * `value: null` = no injected state: the provider fetches /api/capabilities itself.
 * A data router, as in App.tsx (SignalPage uses the router's navigation blocker).
 */
function renderShell(path = "/", value: CapabilitiesState | null = DISABLED) {
  const router = createMemoryRouter(
    [
      {
        element: (
          <CapabilitiesProvider value={value ?? undefined}>
            <Layout />
          </CapabilitiesProvider>
        ),
        children: [
          { index: true, element: <p>home</p> },
          { path: "signal", element: <SignalPage /> },
          { path: "help", element: <HelpPage /> },
        ],
      },
    ],
    { initialEntries: [path] },
  );
  return render(<RouterProvider router={router} />);
}

beforeEach(() => {
  // Default: an API without the signal branch (GET /api/capabilities -> 404).
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "Not Found" }, 404)));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Layout", () => {
  it("renders the tab navigation, the API docs link and the license footer (no phase labels, no health indicator)", () => {
    renderShell();
    expect(screen.getByTestId("nav-sequence")).toHaveAttribute("href", "/");
    expect(screen.getByTestId("nav-help")).toHaveAttribute("href", "/help");
    expect(screen.getByTestId("nav-docs")).toHaveAttribute("href", "/docs");
    expect(screen.queryByTestId("health-indicator")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/phase\s*[12]/i);
    expect(document.body.textContent).not.toMatch(/model ready|model loading/i);

    const footer = screen.getByTestId("footer-license");
    expect(footer).toHaveTextContent(/MIT License/);
    expect(footer).toHaveTextContent(/MultiRM © 2021 Zitao Song/);
    expect(footer).toHaveTextContent(/DirectRM © 2025 Yuxin Zhang/);
    expect(footer).toHaveTextContent(/No account, no cookies, no tracking/);
    expect(footer).toHaveTextContent(/Version 0\.1\.0/);
    expect(footer).not.toHaveTextContent(/API 0\.1\.0/);
  });

  it("hides the Nanopore signal tab when capabilities.signal is false and shows it when true", () => {
    const { unmount } = renderShell("/", DISABLED);
    expect(screen.queryByTestId("nav-signal")).not.toBeInTheDocument();
    unmount();

    renderShell("/", ENABLED);
    expect(screen.getByTestId("nav-signal")).toHaveAttribute("href", "/signal");
    expect(screen.getByTestId("nav-signal")).toHaveTextContent("Nanopore signal");
  });

  it("fetches GET /api/capabilities once at load and enables the tab from the response", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => jsonResponse(capabilitiesFixture));
    vi.stubGlobal("fetch", fetchMock);
    renderShell("/", null);
    expect(await screen.findByTestId("nav-signal")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/capabilities");
  });

  it("treats a 404 from /api/capabilities (older API without the route) as signal=false", async () => {
    renderShell("/signal", null); // beforeEach: fetch -> 404
    expect(await screen.findByTestId("signal-disabled")).toHaveTextContent(
      "The nanopore signal branch is not enabled on this server.",
    );
    expect(screen.queryByTestId("nav-signal")).not.toBeInTheDocument();
    expect(screen.queryByTestId("signal-unavailable")).not.toBeInTheDocument();
  });

  it("a network error is 'could not reach the server' with Retry, not a disabled branch", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) !== "/api/capabilities") return jsonResponse({ detail: "Not Found" }, 404);
      attempts += 1;
      if (attempts === 1) throw new TypeError("Failed to fetch");
      return jsonResponse(capabilitiesFixture);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderShell("/signal", null);
    const notice = await screen.findByTestId("signal-unavailable");
    expect(notice).toHaveTextContent(/Could not reach the server/);
    expect(notice).toHaveTextContent(/Cannot reach the RModHub server/);
    expect(notice).not.toHaveTextContent(/not enabled on this server/);
    expect(screen.queryByTestId("signal-disabled")).not.toBeInTheDocument();
    expect(screen.queryByTestId("nav-signal")).not.toBeInTheDocument();

    // Manual retry: the second answer enables the branch (the provider also retries by itself).
    await user.click(screen.getByTestId("signal-retry"));
    expect(await screen.findByTestId("signal-page")).toBeInTheDocument();
    expect(screen.getByTestId("nav-signal")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter((c) => String(c[0]) === "/api/capabilities")).toHaveLength(2);
  });

  it("does not load anything from another origin", async () => {
    const { container } = renderShell("/help", ENABLED);
    const external = Array.from(container.querySelectorAll("script[src], link[href], img[src], iframe"));
    expect(external).toHaveLength(0);
    const links = Array.from(container.querySelectorAll("a[href^='http']"));
    expect(links.length).toBeGreaterThan(0);
    for (const a of links) {
      expect(a.getAttribute("href")).toMatch(ALLOWED_LINK);
      expect(a).toHaveAttribute("rel", expect.stringContaining("noopener"));
    }
  });
});

describe("LicenseNotice", () => {
  it("states the licenses, both models' authorship and both citations", () => {
    render(
      <MemoryRouter>
        <LicenseNotice />
      </MemoryRouter>,
    );
    const notice = screen.getByTestId("license-notice");
    expect(notice).toHaveTextContent(/MIT License/);
    expect(notice).toHaveTextContent(/© 2021 Zitao Song/);
    expect(notice).toHaveTextContent(/© 2025 Yuxin Zhang/);
    expect(notice).toHaveTextContent(/Nature Communications/);
    expect(notice).toHaveTextContent(/validation/);
    expect(notice).toHaveTextContent(/Remora/);
    expect(notice).toHaveTextContent(/14 days/);
    expect(notice).not.toHaveTextContent(/planned/i);
    expect(screen.getByRole("link", { name: /doi:10\.1038\/s41467-021-24313-3/ })).toHaveAttribute(
      "href",
      "https://doi.org/10.1038/s41467-021-24313-3",
    );
    expect(screen.getByRole("link", { name: /doi:10\.1038\/s41467-025-64495-8/ })).toHaveAttribute(
      "href",
      "https://doi.org/10.1038/s41467-025-64495-8",
    );
  });
});

describe("HelpPage", () => {
  it("exposes every documented anchor, the 12 modification types and the 6 signal types", () => {
    renderShell("/help", ENABLED);
    expect(screen.getByTestId("help-page")).toBeInTheDocument();
    for (const id of HELP_ANCHORS) {
      expect(document.getElementById(id), `#${id}`).not.toBeNull();
    }
    expect(document.getElementById("phase2")).toBeNull();
    expect(document.body.textContent).not.toMatch(/phase\s*2/i);
    const table = document.getElementById("mod-types")!.querySelector("tbody")!;
    expect(table.querySelectorAll("tr")).toHaveLength(12);
    expect(screen.getAllByText(/< 0\.0067/).length).toBeGreaterThan(0);

    const signalTable = screen.getByTestId("signal-mod-types");
    expect(signalTable.querySelectorAll("tr")).toHaveLength(6);
    expect(signalTable).toHaveTextContent("ac4C");
    const section = document.getElementById("nanopore-signal")!;
    expect(section).toHaveTextContent(/--emit-moves/);
    expect(section).toHaveTextContent(/1-based and inclusive/);
    expect(section).toHaveTextContent(/rmodhub\/subset:local/);
    expect(section).toHaveTextContent(/Wilson/);
    expect(section).toHaveTextContent(/6 h/);
    expect(section).toHaveTextContent(/14 days/);
    expect(section).toHaveTextContent(/synthetic/);
    expect(within(section).getByRole("link", { name: "https://github.com/yuxinPenny/DirectRM" })).toBeInTheDocument();
  });
});

describe("SignalPage", () => {
  it("shows the disabled notice (and no form) when the branch is off", () => {
    renderShell("/signal", DISABLED);
    const notice = screen.getByTestId("signal-disabled");
    expect(notice).toHaveTextContent("The nanopore signal branch is not enabled on this server.");
    expect(notice.querySelectorAll("input, form")).toHaveLength(0);
    expect(screen.queryByTestId("signal-page")).not.toBeInTheDocument();
  });

  it("renders the upload form, the sample button and the data-lifecycle notice when enabled", () => {
    renderShell("/signal", ENABLED);
    const page = screen.getByTestId("signal-page");
    for (const slot of ["pod5", "bam", "reference", "regions"]) {
      expect(within(page).getByTestId(`upload-${slot}`)).toHaveAttribute("type", "file");
    }
    expect(within(page).getByTestId("load-sample")).toBeEnabled();
    expect(within(page).getByTestId("run")).toBeDisabled();
    expect(within(page).getByTestId("kit-RNA004")).toBeChecked();
    const lifecycle = within(page).getByTestId("data-lifecycle");
    expect(lifecycle).toHaveTextContent(/pod5 up to 5 GB/);
    expect(lifecycle).toHaveTextContent(/500 MB/);
    expect(lifecycle).toHaveTextContent(/10,000 rows/);
    expect(lifecycle).toHaveTextContent(/1 running and 3 queued/);
    expect(lifecycle).toHaveTextContent(/6 h/);
    expect(lifecycle).toHaveTextContent(/after feature extraction, at most 48 h/);
    expect(lifecycle).toHaveTextContent(/14 days/);
    expect(within(page).getByTestId("subset-command")).toHaveTextContent("docker run --rm");
    expect(page.textContent).not.toMatch(/phase\s*2|coming soon|planned/i);
  });
});
