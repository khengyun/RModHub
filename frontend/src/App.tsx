import { createBrowserRouter, createRoutesFromElements, Navigate, Route, RouterProvider } from "react-router-dom";
import { CapabilitiesProvider } from "./components/layout/CapabilitiesProvider";
import { Layout } from "./components/layout/Layout";
import { HelpPage } from "./pages/HelpPage";
import { ResultPage } from "./pages/ResultPage";
import { SequencePage } from "./pages/SequencePage";
import { SignalPage } from "./pages/SignalPage";

/** Capabilities context around the shell (one fetch per app load). */
function Root() {
  return (
    <CapabilitiesProvider>
      <Layout />
    </CapabilitiesProvider>
  );
}

/**
 * Route table (a data router, so that pages can block in-app navigation with
 * `useBlocker` while an upload is running).
 *   /                 sequence branch (MultiRM): paste -> POST /api/predict/sequence
 *   /signal           nanopore signal branch (DirectRM): upload -> job; a notice when the
 *                     server reports capabilities.signal = false
 *   /result/:jobId    public, bookmarkable job page: polls GET /api/jobs/:jobId and renders
 *                     the SAME results components (ResultsTable, TrackView) from ModSite rows
 *   /help, /sequence -> /, anything else -> /
 */
export const appRoutes = createRoutesFromElements(
  <Route element={<Root />}>
    <Route index element={<SequencePage />} />
    <Route path="sequence" element={<Navigate to="/" replace />} />
    <Route path="signal" element={<SignalPage />} />
    <Route path="result/:jobId" element={<ResultPage />} />
    <Route path="help" element={<HelpPage />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Route>,
);

const router = createBrowserRouter(appRoutes);

export default function App() {
  return <RouterProvider router={router} />;
}
