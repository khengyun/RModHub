import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/layout/Layout";
import { SequencePage } from "./pages/SequencePage";
import { HelpPage } from "./pages/HelpPage";
import { SignalPage } from "./pages/SignalPage";

/**
 * Route table. Phase 1 = sequence branch only.
 *
 * Reserved for the nanopore/DirectRM branch (phase 2 backend + frontend):
 *   /signal              upload BAM + move table -> POST /api/predict/signal -> job id
 *   /result/:jobId       poll GET /api/jobs/:jobId, then render the SAME results
 *                        components (ResultsTable, TrackView) from the shared ModSite rows.
 * `SignalPage` is a placeholder so the navigation already has its second tab.
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<SequencePage />} />
        <Route path="sequence" element={<Navigate to="/" replace />} />
        <Route path="signal" element={<SignalPage />} />
        <Route path="help" element={<HelpPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
