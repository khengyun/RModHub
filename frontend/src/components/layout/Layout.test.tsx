import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import health from "../../api/fixtures/health.json";
import { HelpPage } from "../../pages/HelpPage";
import { SignalPage } from "../../pages/SignalPage";
import { Layout } from "./Layout";
import { LicenseNotice } from "./LicenseNotice";

/** Anchor ids the Help page must expose (UI contract with tooltips / E2E tests). */
const HELP_ANCHORS = [
  "quick-start", "input", "reading-results", "flanks", "mod-types", "multiple-mods",
  "track-view", "table-and-csv", "limits", "citation", "privacy", "phase2",
];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderShell(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<p>home</p>} />
          <Route path="signal" element={<SignalPage />} />
          <Route path="help" element={<HelpPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(health)));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Layout", () => {
  it("renders the tab navigation, the API docs link and the license footer", async () => {
    renderShell();
    expect(screen.getByTestId("nav-sequence")).toHaveAttribute("href", "/");
    expect(screen.getByTestId("nav-signal")).toHaveAttribute("href", "/signal");
    expect(screen.getByTestId("nav-signal")).toHaveTextContent(/phase 2/i);
    expect(screen.getByTestId("nav-help")).toHaveAttribute("href", "/help");
    expect(screen.getByTestId("nav-docs")).toHaveAttribute("href", "/docs");

    const footer = screen.getByTestId("footer-license");
    expect(footer).toHaveTextContent(/MIT License/);
    expect(footer).toHaveTextContent(/MultiRM © 2021 Zitao Song/);
    expect(footer).toHaveTextContent(/No account, no cookies, no tracking/);
    expect(footer).toHaveTextContent(/Version 0\.1\.0/);

    // Health indicator resolves to "ready" from the fixture and stops polling.
    expect(await screen.findByText("model ready")).toBeInTheDocument();
    expect(screen.getByTestId("health-indicator")).toHaveAttribute("data-status", "ready");
    expect(footer).toHaveTextContent(/API 0\.1\.0/);
  });

  it("shows 'model loading…' while /health answers 503", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "model not loaded" }, 503)));
    renderShell();
    expect(await screen.findByText("model loading…")).toBeInTheDocument();
  });

  it("does not load anything from another origin", async () => {
    const { container } = renderShell("/help");
    await screen.findByText("model ready");
    const external = Array.from(container.querySelectorAll("script[src], link[href], img[src], iframe"));
    expect(external).toHaveLength(0);
    // Hyperlinks are allowed, but only to the model repo, its paper and the MIT text.
    for (const a of Array.from(container.querySelectorAll("a[href^='http']"))) {
      expect(a.getAttribute("href")).toMatch(
        /^https:\/\/(github\.com\/Tsedao\/MultiRM|doi\.org\/10\.1038\/s41467-021-24313-3|opensource\.org\/licenses\/MIT)/,
      );
      expect(a).toHaveAttribute("rel", expect.stringContaining("noopener"));
    }
  });
});

describe("LicenseNotice", () => {
  it("states the license, the model authorship and the citation", () => {
    render(
      <MemoryRouter>
        <LicenseNotice />
      </MemoryRouter>,
    );
    const notice = screen.getByTestId("license-notice");
    expect(notice).toHaveTextContent(/MIT License/);
    expect(notice).toHaveTextContent(/© 2021 Zitao Song/);
    expect(notice).toHaveTextContent(/Nature Communications/);
    expect(notice).toHaveTextContent(/validation/);
    expect(screen.getByRole("link", { name: /doi:10\.1038/ })).toHaveAttribute(
      "href",
      "https://doi.org/10.1038/s41467-021-24313-3",
    );
  });
});

describe("HelpPage", () => {
  it("exposes every documented anchor and the 12 modification types", async () => {
    renderShell("/help");
    await screen.findByText("model ready");
    expect(screen.getByTestId("help-page")).toBeInTheDocument();
    for (const id of HELP_ANCHORS) {
      expect(document.getElementById(id), `#${id}`).not.toBeNull();
    }
    const table = document.getElementById("mod-types")!.querySelector("tbody")!;
    expect(table.querySelectorAll("tr")).toHaveLength(12);
    expect(screen.getAllByText(/< 0\.0067/).length).toBeGreaterThan(0);
  });
});

describe("SignalPage", () => {
  it("has no form controls", async () => {
    renderShell("/signal");
    await screen.findByText("model ready");
    const page = screen.getByTestId("signal-page");
    expect(page.querySelectorAll("input, textarea, button, form")).toHaveLength(0);
  });
});
