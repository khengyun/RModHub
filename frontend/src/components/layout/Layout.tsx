/**
 * Application shell: header (inline-SVG logo, tab navigation), the routed page (<Outlet />)
 * and the license footer on every page. No external assets anywhere: the logo is inline
 * SVG and fonts come from the system stack in index.css.
 *
 * The "Nanopore signal" tab is shown only when GET /api/capabilities reports signal: true.
 */
import { NavLink, Outlet } from "react-router-dom";
import { useCapabilities } from "./CapabilitiesProvider";
import { SiteFooter } from "./SiteFooter";

function tabClass({ isActive }: { isActive: boolean }): string {
  return [
    "inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-1 pb-2.5 pt-3 text-sm font-medium",
    isActive
      ? "border-brand-600 text-brand-800"
      : "border-transparent text-slate-600 hover:border-slate-300 hover:text-slate-900",
  ].join(" ");
}

function Logo() {
  return (
    <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true" className="shrink-0">
      <rect x="1" y="1" width="30" height="30" rx="7" fill="#1f4e79" />
      {/* an RNA strand ... */}
      <path
        d="M5 20 C 9 8, 13 8, 16 16 S 23 24, 27 12"
        fill="none"
        stroke="#ffffff"
        strokeWidth="2.2"
        strokeLinecap="round"
      />
      {/* ... with one modified base */}
      <circle cx="16" cy="16" r="3.2" fill="#e6ab02" stroke="#1f4e79" strokeWidth="1" />
    </svg>
  );
}

export function Layout() {
  const { capabilities } = useCapabilities();

  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow"
      >
        Skip to content
      </a>

      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-8 gap-y-1 px-4">
          <NavLink to="/" className="flex items-center gap-2 py-3" aria-label="RModHub home">
            <Logo />
            <span className="leading-tight">
              <span className="block text-lg font-semibold text-brand-800">RModHub</span>
              <span className="block text-[11px] text-slate-500">RNA modification site prediction</span>
            </span>
          </NavLink>

          <nav aria-label="Primary" className="flex items-center gap-6">
            <NavLink to="/" end className={tabClass} data-testid="nav-sequence">
              Sequence
            </NavLink>
            {capabilities.signal && (
              <NavLink to="/signal" className={tabClass} data-testid="nav-signal">
                Nanopore signal
              </NavLink>
            )}
            <NavLink to="/help" className={tabClass} data-testid="nav-help">
              Help
            </NavLink>
            {/* Served by the API (self-hosted Swagger UI), same origin: a plain link, not a route. */}
            <a href="/docs" className={tabClass({ isActive: false })} data-testid="nav-docs">
              API docs
            </a>
          </nav>
        </div>
      </header>

      <main id="main" className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
        <Outlet />
      </main>

      <SiteFooter />
    </div>
  );
}
