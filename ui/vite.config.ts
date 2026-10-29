// The dashboard is built into the Python package (src/vatf/web/static), so `uv run vatf ui`
// serves it with no Node installed. `npm run dev` serves it live and forwards the API to a
// running `uv run vatf ui`.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const backend = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/",
  build: {
    outDir: "../src/vatf/web/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: false, headers: { Host: "127.0.0.1:8765" } },
      "/runs": { target: backend, headers: { Host: "127.0.0.1:8765" } },
    },
  },
});
