import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local FastAPI workbench started with `uv run forgeflow api --port 8000`.
// FORGEFLOW_API_PROXY exists only to point a dev server at another local API.
const API_TARGET = process.env.FORGEFLOW_API_PROXY?.trim() || "http://127.0.0.1:8000";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: API_TARGET,
        // Keep the browser's Host/Origin so the API's trust fence accepts the
        // dev server as the same local authority.  Rewriting it yields 403.
        changeOrigin: false,
        configure(proxy) {
          // A stopped API otherwise looks like an empty response or Vite's SPA
          // fallback.  Answer with a real 502 so the browser fails loudly.
          proxy.on("error", (error, _request, response) => {
            const detail =
              `ForgeFlow API is not reachable at ${API_TARGET} (${error.message}). ` +
              "Start it with: uv run forgeflow api --host 127.0.0.1 --port 8000";
            console.error(`[forgeflow] ${detail}`);
            if ("writeHead" in response) {
              if (!response.headersSent) {
                response.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
                response.end(JSON.stringify({ detail }));
              } else {
                response.end();
              }
            } else {
              response.destroy();
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
