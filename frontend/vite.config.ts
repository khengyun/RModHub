/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Backend during `vite dev`. In Docker, nginx proxies the same paths (see nginx.conf).
const API_TARGET = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Playwright writes its report/traces next to the sources; do not let the dev server
    // reload the page under test when that happens.
    watch: { ignored: ["**/playwright-report/**", "**/test-results/**", "**/blob-report/**"] },
    proxy: Object.fromEntries(
      ["/api", "/health", "/docs", "/openapi.json", "/static"].map((p) => [
        p,
        { target: API_TARGET, changeOrigin: true },
      ]),
    ),
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // Everything must be bundled locally: no CDN, no external font/script requests.
    // `npm run check:no-cdn` greps dist/ after the build to enforce it.
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
