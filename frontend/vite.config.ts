import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The UI talks to the backend through /api, proxied in dev so REST and the
// WebSocket share one origin. Set VITE_API_BASE to call a backend directly.
const backend = process.env.CODELOOP_BACKEND ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: backend,
        ws: true,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
